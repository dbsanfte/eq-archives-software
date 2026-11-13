import { relevanceSearch } from './Reranking';
import { captureKey } from './CaptureIdentity';
import { discussionIdentity, discussionMatchKey } from './DiscussionIdentity';

export const CAPTURE_WINDOW = 1000;
const BATCH_SIZE = 50;

// Scan the existing bounded result window incrementally. One cache per connector
// keeps groups unique across pages without reindexing the archive or downloading
// full documents. Representatives retain the user's search/sort order.
export function createGroupedSearch(search, getContext, rank) {
  let active;
  let latestRequest = 0;
  return async (state, queryConfig) => {
    const request = ++latestRequest;
    const context = getContext(state);
    const ranked = rank && relevanceSearch(state);
    if (context.enabled === false && !ranked) {
      active = null;
      try { return await search(state, queryConfig); } catch (error) {
        if (request === latestRequest) throw error;
        return { results: [], totalResults: 0 };
      }
    }
    const { current = 1, resultsPerPage = 20, searchTerm, filters, sortField, sortDirection, sortList } = state;
    const key = JSON.stringify({ searchTerm, filters, sortField, sortDirection, sortList, queryConfig, context });
    if (!active || active.key !== key) {
      active = { key, groups: new Map(), scanned: 0, done: false, response: {}, flight: null };
    }
    const cache = active;
    const end = current * resultsPerPage;
    while (active === cache && !cache.done && cache.groups.size <= Math.max(end, ranked ? 20 : 0)) {
      if (!cache.flight) {
        cache.flight = (async () => {
          const response = await search({ ...state, current: cache.scanned / BATCH_SIZE + 1, resultsPerPage: BATCH_SIZE }, queryConfig);
          cache.response = response;
          response.results.forEach((result, i) => {
            const discussion = context.enabled === false ? null : discussionIdentity(result);
            const id = context.enabled === false ? result._meta?.id || `row:${cache.scanned + i}` : discussion?.key || captureKey(result, cache.scanned + i);
            if (!cache.groups.has(id)) cache.groups.set(id, { result, discussion, matches: new Map() });
            const group = cache.groups.get(id);
            if (discussion) {
              const match = discussionMatchKey(result);
              if (match && !group.matches.has(match)) group.matches.set(match, result);
            }
          });
          cache.scanned += response.results.length;
          cache.done = response.results.length < BATCH_SIZE || cache.scanned >= response.totalResults || cache.scanned >= CAPTURE_WINDOW;
        })();
      }
      const flight = cache.flight;
      try { await flight; } catch (error) {
        // Search UI ignores stale successes, but surfaces stale rejections. Stop
        // obsolete scans quietly so an old failure cannot cover newer results.
        if (request === latestRequest) throw error;
        break;
      } finally {
        // Another waiter may already have started the next batch. Only clear
        // the promise this request awaited, or parallel scans can skip records.
        if (cache.flight === flight) cache.flight = null;
      }
    }
    let ordered = Array.from(cache.groups.values());
    if (ranked && ordered.length) {
      if (!cache.ranking) {
        const head = ordered.slice(0, 20);
        cache.ranking = rank(searchTerm, head.map(group => group.result)).then(results => {
          const groups = new Map(head.map(group => [group.result, group]));
          return results.map(result => groups.get(result));
        });
      }
      ordered = [...await cache.ranking, ...ordered.slice(20)];
    }
    if (context.enabled === false) {
      const totalPages = Math.max(1, Math.ceil(Math.min(CAPTURE_WINDOW, cache.response.totalResults) / resultsPerPage));
      const page = Math.min(current, totalPages);
      const start = (page - 1) * resultsPerPage;
      const results = ordered.slice(start, start + resultsPerPage).map(group => group.result);
      return { ...cache.response, results, current: page, totalPages,
        pagingStart: results.length ? start + 1 : 0, pagingEnd: start + results.length };
    }
    // Old links may point past the final page once duplicate captures collapse.
    const page = cache.done ? Math.min(current, Math.max(1, Math.ceil(cache.groups.size / resultsPerPage))) : current;
    const start = (page - 1) * resultsPerPage;
    const results = ordered.slice(start, start + resultsPerPage).map(group =>
      group.discussion ? { ...group.result, _discussionGroup: { ...group.discussion,
        matches: Array.from(group.matches.values()), scanned: cache.scanned } } : group.result);
    const hasNext = cache.groups.size > start + resultsPerPage;
    return {
      ...cache.response,
      results,
      current: page,
      pagingStart: results.length ? start + 1 : 0,
      pagingEnd: start + results.length,
      totalPages: hasNext ? page + 1 : page,
      rawResponse: { captureGroups: {
        hasNext,
        scanned: cache.scanned,
        windowReached: cache.scanned >= CAPTURE_WINDOW && cache.response.totalResults > CAPTURE_WINDOW
      } }
    };
  };
}
