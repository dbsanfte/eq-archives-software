import { captureKey } from './CaptureIdentity';

export const CAPTURE_WINDOW = 1000;
const BATCH_SIZE = 50;

// Scan the existing bounded result window incrementally. One cache per connector
// keeps groups unique across pages without reindexing the archive or downloading
// full documents. Representatives retain the user's search/sort order.
export function createGroupedSearch(search, getContext) {
  let active;
  let latestRequest = 0;
  return async (state, queryConfig) => {
    const request = ++latestRequest;
    const context = getContext(state);
    if (context.enabled === false) {
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
    while (active === cache && !cache.done && cache.groups.size <= end) {
      if (!cache.flight) {
        cache.flight = (async () => {
          const response = await search({ ...state, current: cache.scanned / BATCH_SIZE + 1, resultsPerPage: BATCH_SIZE }, queryConfig);
          cache.response = response;
          response.results.forEach((result, i) => {
            const id = captureKey(result, cache.scanned + i);
            if (!cache.groups.has(id)) cache.groups.set(id, result);
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
    // Old links may point past the final page once duplicate captures collapse.
    const page = cache.done ? Math.min(current, Math.max(1, Math.ceil(cache.groups.size / resultsPerPage))) : current;
    const start = (page - 1) * resultsPerPage;
    const results = Array.from(cache.groups.values()).slice(start, start + resultsPerPage);
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
