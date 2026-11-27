import { safeSearchReturnUrl } from '../views/reader/reader-utils';

const storageKey = 'eqarchives.reader-return.v1';
const maxAge = 60 * 60 * 1000;
const maxLength = 2 * 1024 * 1024;

// The return URL identifies the history entry. The request key also checks
// that the entry's query has caught up with the completed search.
export function searchPageKey({ searchTerm = '', filters = [], current = 1, resultsPerPage = 20,
  sortField, sortDirection, sortList }) {
  const sort = sortList?.length ? sortList : sortField ? [{ field: sortField, direction: sortDirection }] : [];
  return JSON.stringify([searchTerm, filters, current, resultsPerPage, sort], (_, value) =>
    value && typeof value === 'object' && !Array.isArray(value)
      ? Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]])) : value);
}

function resultsUrl() {
  return window.location.pathname + window.location.search;
}

export function forgetReaderReturn() {
  try { window.sessionStorage.removeItem(storageKey); } catch (_) { /* Storage is optional. */ }
}

export function readReaderReturn() {
  try {
    const stored = window.sessionStorage.getItem(storageKey);
    if (!stored || stored.length > maxLength) return null;
    const entry = JSON.parse(stored);
    return entry?.url === resultsUrl() && entry.expires > Date.now() && typeof entry.request === 'string' &&
      Array.isArray(entry.response?.results) && entry.response.results.length ? entry : null;
  } catch (_) { return null; }
}

export function cacheReaderReturn(event, { phase, display, request }, parameters) {
  if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  const anchor = event.target.closest?.('a[href]');
  if (!anchor || (anchor.target && anchor.target !== '_self') || anchor.hasAttribute('download')) return;
  const reader = new URL(anchor.href, window.location.origin);
  const url = resultsUrl();
  if (reader.origin !== window.location.origin || reader.pathname !== '/document' ||
      safeSearchReturnUrl(reader.searchParams.get('return')) !== url) return;

  forgetReaderReturn();
  if (phase !== 'idle' || !display?.results.length || !request) return;
  const { results, facets, totalResults, totalPages, current, pagingStart, pagingEnd, requestId, rawResponse } = display;
  const response = { results, facets, totalResults, totalPages, current, pagingStart, pagingEnd, requestId,
    rawResponse: { captureGroups: rawResponse?.captureGroups } };
  try {
    const stored = JSON.stringify({ url, request, response, parameters, expires: Date.now() + maxAge });
    if (stored.length <= maxLength) window.sessionStorage.setItem(storageKey, stored);
  } catch (_) { /* The reader can still return to a fresh search. */ }
}
