import { relevanceSearch } from './Reranking';
import { usesQuerySyntax } from './QueryPolicy';
import { CAPTURE_WINDOW } from './GroupedSearch';

export const FUSION_WINDOW = 100;
const TAIL_BATCH = 50;
const idOf = result => result._meta.id;

// Match Unicode code-point ordering in the offline evaluator, including ties.
function compareIds(a, b) {
  const left = Array.from(a), right = Array.from(b);
  for (let n = 0; n < Math.min(left.length, right.length); n++) {
    const difference = left[n].codePointAt(0) - right[n].codePointAt(0);
    if (difference) return difference;
  }
  return left.length - right.length;
}

export function fuseResults(lexical, semantic) {
  const scores = new Map(), documents = new Map();
  for (const results of [lexical, semantic]) {
    const seen = new Set();
    results.slice(0, FUSION_WINDOW).forEach((result, n) => {
      const id = idOf(result);
      if (seen.has(id)) return;
      seen.add(id);
      scores.set(id, (scores.get(id) || 0) + 1 / (61 + n));
      if (!documents.has(id)) documents.set(id, result);
    });
  }
  return Array.from(scores.keys()).sort((a, b) => scores.get(b) - scores.get(a) || compareIds(a, b))
    .map(id => documents.get(id));
}

export function createPreparedSearch(search, getContext, prepare) {
  let latest = 0, preparation;
  return async (state, queryConfig) => {
    const request = ++latest;
    const { embedding, ...scope } = getContext(state);
    const { searchTerm, filters, sortField, sortDirection, sortList } = state;
    const key = JSON.stringify({ searchTerm, filters, sortField, sortDirection, sortList, queryConfig, scope });
    if (!preparation || preparation.key !== key) {
      preparation?.controller.abort();
      preparation = { key, controller: new AbortController() };
      if (relevanceSearch(state) && scope.params.enableSemanticSearch &&
          !embedding && !usesQuerySyntax(state.searchTerm)) {
        const signal = preparation.controller.signal;
        preparation.promise = (async () => {
          try { await prepare(state.searchTerm, { signal }); } catch (_) {
            // Keep a failed preparation stable while paging through this search.
          }
        })();
      }
    }
    await preparation.promise;
    if (request !== latest) return { results: [], totalResults: 0 };
    return search(state, queryConfig);
  };
}

// Fuse a fixed head, then continue through lexical results without repeating IDs.
// The outer grouping/reranking layer still chooses representatives and pages.
export function createFusedSearch(search, getContext) {
  let active;
  return async (state, queryConfig) => {
    const context = getContext(state);
    if (!relevanceSearch(state) || !context.params.enableSemanticSearch ||
        !context.embedding || usesQuerySyntax(state.searchTerm)) {
      active = null;
      return search(state, queryConfig);
    }
    const { searchTerm, filters, sortField, sortDirection, sortList } = state;
    const key = JSON.stringify({ searchTerm, filters, sortField, sortDirection, sortList, context, queryConfig });
    if (!active || active.key !== key) active = { key, rows: [], seen: new Set(), offset: FUSION_WINDOW };
    const cache = active;
    if (!cache.head) {
      cache.head = (async () => {
        cache.response = await search({ ...state, current: 1, resultsPerPage: FUSION_WINDOW,
          _archiveRetrieval: 'lexical' }, queryConfig);
        cache.lexicalTotal = cache.response.totalResults;
        let semantic = [];
        let semanticAvailable = false;
        try {
          const response = await search({ ...state, current: 1, resultsPerPage: FUSION_WINDOW,
            _archiveRetrieval: 'semantic' }, { ...queryConfig, facets: {} });
          semantic = response.results;
          semanticAvailable = true;
        } catch (_) {
          // Keep this lexical fallback for the entire paging session.
        }
        cache.rows = fuseResults(cache.response.results, semantic);
        cache.seen = new Set(cache.rows.map(idOf));
        cache.upper = Math.min(CAPTURE_WINDOW + 1,
          cache.response.totalResults + cache.rows.length - cache.response.results.length);
        cache.done = cache.response.results.length < FUSION_WINDOW || cache.offset >= cache.response.totalResults;
        if (semanticAvailable) {
          try {
            const metadata = await search({ ...state, current: 1, resultsPerPage: 1,
              _archiveRetrieval: 'metadata' }, queryConfig);
            // Facets describe the keyword/source-vector union, not either leg alone.
            cache.response = { ...cache.response, ...metadata };
          } catch (_) { /* Retain keyword facets during a temporary metadata outage. */ }
        }
      })();
    }
    try { await cache.head; } catch (error) {
      if (active === cache) active = null;
      throw error;
    }
    const { current = 1, resultsPerPage = 20 } = state;
    const start = (current - 1) * resultsPerPage;
    const end = Math.min(CAPTURE_WINDOW, start + resultsPerPage);
    while (!cache.done && cache.rows.length < end) {
      if (!cache.flight) {
        cache.flight = (async () => {
          const response = await search({ ...state, current: cache.offset / TAIL_BATCH + 1,
            resultsPerPage: TAIL_BATCH, _archiveRetrieval: 'lexical' }, queryConfig);
          for (const result of response.results) {
            const id = idOf(result);
            if (!cache.seen.has(id)) { cache.seen.add(id); cache.rows.push(result); }
          }
          cache.offset += response.results.length;
          cache.done = response.results.length < TAIL_BATCH || cache.offset >= cache.lexicalTotal ||
            cache.offset >= CAPTURE_WINDOW || cache.rows.length >= CAPTURE_WINDOW;
        })();
      }
      const flight = cache.flight;
      try { await flight; } finally { if (cache.flight === flight) cache.flight = null; }
    }
    const truncated = cache.rows.length > CAPTURE_WINDOW || cache.offset >= CAPTURE_WINDOW && cache.offset < cache.lexicalTotal;
    return { ...cache.response, results: cache.rows.slice(start, end),
      totalResults: cache.done && !truncated ? cache.rows.length : cache.upper };
  };
}
