import { createContext } from 'react';

export const ReaderSearchContext = createContext('');
export const searchPhrase = term => (term || '').replace(/^"(.*)"$/, '$1');
export const recordId = result => result?._meta?.id || result?.id?.raw || '';
export const value = (result, field) => typeof result?.[field]?.raw === 'string' ? result[field].raw : '';

// Reader return links only restore a search on this site, never an arbitrary URL.
export function safeSearchReturnUrl(value) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return '/';
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && url.pathname === '/' && !url.hash
      ? url.pathname + url.search : '/';
  } catch (_) { return '/'; }
}

// Search UI updates history after rendering results. Refresh the link at
// activation time so it carries the completed query, filters, sort and page.
export function refreshSearchReturn(event) {
  if (window.location.pathname !== '/') return;
  const url = new URL(event.currentTarget.href, window.location.origin);
  if (url.origin !== window.location.origin || url.pathname !== '/document') return;
  const returnTo = safeSearchReturnUrl(window.location.pathname + window.location.search);
  if (returnTo === '/') url.searchParams.delete('return');
  else url.searchParams.set('return', returnTo);
  event.currentTarget.setAttribute('href', url.pathname + url.search + url.hash);
}

export const searchReturnLinkHandlers = {
  onClick: refreshSearchReturn,
  onAuxClick: refreshSearchReturn,
  onContextMenu: refreshSearchReturn,
  onFocus: refreshSearchReturn,
  onMouseEnter: refreshSearchReturn
};

export function readerUrl(id, { compare = '', find = '', part = '', returnTo = '' } = {}) {
  const params = new URLSearchParams({ id });
  if (compare) params.set('compare', compare);
  if (find) params.set('find', find);
  if (part) params.set('part', part);
  const safeReturn = safeSearchReturnUrl(returnTo);
  if (safeReturn !== '/') params.set('return', safeReturn);
  return `/document?${params}`;
}

// Archive timestamps without an explicit zone are UTC, including older records.
export function comparisonUrl(selected, other, { returnTo = '' } = {}) {
  const time = record => {
    const date = value(record, 'capture_date');
    return Date.parse(/T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(date) ? date + 'Z' : date);
  };
  const [from, to] = time(other) < time(selected) ? [other, selected] : [selected, other];
  return readerUrl(recordId(from), { compare: recordId(to), returnTo });
}

// Resolve relative source links against the original page, retaining its capture.
export function sourceLink(href, base = '') {
  if (!href) return undefined;
  try {
    const capture = base.match(/^(https?:\/\/web\.archive\.org\/web\/\d{14}(?:[a-z]+_)?\/)(https?:\/\/.+)$/i);
    const relative = !/^[a-z][a-z\d+.-]*:/i.test(href) && !href.startsWith('//');
    const url = new URL(href, capture ? capture[2] : base || undefined);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return undefined;
    return capture && relative ? capture[1] + url.href : url.href;
  } catch (_) { return undefined; }
}

export function citation(result, link) {
  const pieces = [value(result, 'title') || 'Untitled document'];
  if (value(result, 'capture_date')) pieces.push(`Captured ${value(result, 'capture_date')} (archive timestamp)`);
  const source = sourceLink(value(result, 'url'));
  if (source) pieces.push(source);
  pieces.push(`EQ Archives: ${link}`);
  return pieces.join('. ');
}

export function downloadText(text, title) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `${(title || 'archive-document').replace(/[^a-z\d_-]+/gi, '-').slice(0, 80)}.txt`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const MAX_MARKS = 1000;
export function highlightParts(text, term, budget = { remaining: MAX_MARKS }) {
  if (!term || !budget.remaining) return [{ text, match: false }];
  const regex = new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu');
  const parts = [];
  let start = 0, found;
  while (budget.remaining && (found = regex.exec(text))) {
    if (found.index > start) parts.push({ text: text.slice(start, found.index), match: false });
    parts.push({ text: found[0], match: true });
    start = found.index + found[0].length;
    budget.remaining -= 1;
  }
  if (start < text.length) parts.push({ text: text.slice(start), match: false });
  return parts;
}

export function highlightMarkdown({ term }) {
  return tree => {
    const budget = { remaining: MAX_MARKS };
    function visit(node) {
      if (!node.children) return;
      node.children = node.children.flatMap(child => {
        if (child.type !== 'text') { visit(child); return [child]; }
        return highlightParts(child.value, term, budget).map(part => part.match ? {
          type: 'element', tagName: 'mark', properties: { 'data-reader-match': true },
          children: [{ type: 'text', value: part.text }]
        } : { type: 'text', value: part.text });
      });
    }
    visit(tree);
  };
}
