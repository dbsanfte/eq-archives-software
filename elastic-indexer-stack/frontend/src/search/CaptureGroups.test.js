import { captureIdentity, captureKey } from './CaptureIdentity';
import { createGroupedSearch, CAPTURE_WINDOW } from './GroupedSearch';

const record = (path, stamp = '20000101000000', extra = {}) => ({
  _meta: { id: `websites/example.org/${stamp}/${path}` },
  url: { raw: `https://web.archive.org/web/${stamp}/http://example.org/${path}` },
  title: { raw: path }, ...extra
});
const state = { current: 1, resultsPerPage: 2, searchTerm: 'cyclops', filters: [] };
const makeSearch = records => jest.fn(async s => ({
  results: records.slice((s.current - 1) * s.resultsPerPage, s.current * s.resultsPerPage),
  totalResults: records.length, facets: { domains: ['preserved'] }, resultSearchTerm: s.searchTerm
}));

test('groups Wayback captures by exact original page, retaining query strings and path case', () => {
  expect(captureKey(record('thread?id=1'))).toBe(captureKey(record('thread?id=1', '20020101000000')));
  for (const path of ['thread?id=2', 'Thread?id=1', 'thread/', 'thread']) {
    expect(captureKey(record(path))).not.toBe(captureKey(record('thread?id=1')));
  }
  expect(captureIdentity(record('thread?id=1')).originalUrl).toBe('http://example.org/thread?id=1');
});

test('keeps distinct posts, unknown URLs, malformed captures, and attachments separate', () => {
  expect(captureIdentity({ _meta: { id: 'newsgroups/1.txt' }, url: { raw: 'https://example.org/post' } })).toBeNull();
  expect(captureKey({ _meta: { id: 'a' } })).not.toBe(captureKey({ _meta: { id: 'b' } }));
  expect(captureIdentity(record('a', 'not-a-date'))).toBeNull();
  expect(captureIdentity(record('a', undefined, { parent_id: { raw: 'parent' } }))).toBeNull();
  expect(captureIdentity({ url: { raw: 'javascript:alert(1)' } })).toBeNull();
});

test('recognizes replay modifiers, fragments, and legacy records without a source ID', () => {
  const item = record('a');
  item.url.raw = 'https://web.archive.org/web/20000101000000id_/https://EXAMPLE.org/a#section';
  expect(captureIdentity(item).originalUrl).toBe('https://example.org/a');
  expect(captureIdentity({ _meta: { id: item._meta.id } }).originalUrl).toBe('http://example.org/a');
  expect(captureKey(record('a'))).not.toBe(captureKey(item));
});

test('fills pages with unique groups and never repeats a group on later pages', async () => {
  const records = [record('a'), ...Array.from({ length: 58 }, (_, i) => record('a', String(20000101000001 + i))),
    record('b'), record('c'), record('a', '20010101000000'), record('d')];
  const fetch = makeSearch(records);
  const search = createGroupedSearch(fetch, () => ({}));
  const first = await search(state, {});
  expect(first.results.map(r => r.title.raw)).toEqual(['a', 'b']);
  expect(first.results[0]._meta.id).toBe(records[0]._meta.id);
  expect(first.rawResponse.captureGroups.hasNext).toBe(true);
  expect(first.totalResults).toBe(records.length);
  expect(first.facets).toEqual({ domains: ['preserved'] });
  const second = await search({ ...state, current: 2 }, {});
  expect(second.results.map(r => r.title.raw)).toEqual(['c', 'd']);
  expect(second.rawResponse.captureGroups.hasNext).toBe(false);
  const count = fetch.mock.calls.length;
  expect((await search(state, {})).results).toEqual(first.results);
  expect(fetch).toHaveBeenCalledTimes(count);
});

test('invalidates grouped results when filters, sorting, or semantic settings change', async () => {
  const fetch = makeSearch([record('a')]);
  let context = { params: { semantic: true } };
  const search = createGroupedSearch(fetch, () => context);
  await search(state, {});
  await search({ ...state, filters: [{ field: 'domain_name', values: ['example.org'] }] }, {});
  await search({ ...state, sortField: 'capture_date', sortDirection: 'asc' }, {});
  context = { params: { semantic: false }, dateFilters: { from: '2001' } };
  await search(state, {});
  expect(fetch).toHaveBeenCalledTimes(4);
});

test('bounds scans at the archive window and exposes its limit without phantom pages', async () => {
  const fetch = makeSearch(Array.from({ length: CAPTURE_WINDOW + 10 }, (_, i) => record('a', String(20000101000000 + i))));
  const search = createGroupedSearch(fetch, () => ({}));
  const result = await search(state, {});
  expect(result.results).toHaveLength(1);
  expect(result.rawResponse.captureGroups).toMatchObject({ hasNext: false, windowReached: true, scanned: CAPTURE_WINDOW });
  expect(fetch.mock.calls.every(([s]) => s.current * s.resultsPerPage <= CAPTURE_WINDOW)).toBe(true);
});

test('supports empty results, unknown IDs, all-capture mode, and retries a failed batch', async () => {
  const fetch = makeSearch([]);
  let enabled = true;
  const search = createGroupedSearch(fetch, () => ({ enabled }));
  expect((await search(state, {})).results).toEqual([]);
  enabled = false;
  expect((await search(state, {})).rawResponse).toBeUndefined();
  enabled = true;
  fetch.mockRejectedValueOnce(new Error('offline'));
  await expect(search(state, {})).rejects.toThrow('offline');
  fetch.mockResolvedValueOnce({ results: [{ title: { raw: 'Unknown A' } }, { title: { raw: 'Unknown B' } }], totalResults: 2 });
  expect((await search(state, {})).results).toHaveLength(2);
});

test('a superseded search cannot pollute or keep fetching for the newer search', async () => {
  let resolve;
  const fetch = makeSearch([record('new')]);
  fetch.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  const search = createGroupedSearch(fetch, () => ({}));
  const old = search(state, {});
  const latest = await search({ ...state, searchTerm: 'new' }, {});
  resolve({ results: Array.from({ length: 50 }, () => record('old')), totalResults: 1000 });
  await old;
  expect(latest.results[0].title.raw).toBe('new');
  expect((await search({ ...state, searchTerm: 'new' }, {})).results[0].title.raw).toBe('new');
  expect(fetch).toHaveBeenCalledTimes(2);
});

test('overlapping requests for the same search share a batch', async () => {
  let resolve;
  const fetch = jest.fn(() => new Promise(done => { resolve = done; }));
  const search = createGroupedSearch(fetch, () => ({}));
  const first = search(state, {});
  const second = search(state, {});
  resolve({ results: [record('a')], totalResults: 1 });
  expect((await first).results).toEqual((await second).results);
  expect(fetch).toHaveBeenCalledTimes(1);
});

test('rejects malformed original URLs and never groups credential-bearing URLs', () => {
  for (const original of ['http://[invalid', 'http://name:password@example.org/a']) {
    expect(captureIdentity({ url: { raw: `https://web.archive.org/web/20000101000000/${original}` } })).toBeNull();
  }
});

test('old bookmarks beyond the grouped pages land on the last available page', async () => {
  const search = createGroupedSearch(makeSearch([record('a'), record('a', '20010101000000'), record('b'), record('c')]), () => ({}));
  const result = await search({ ...state, current: 50 }, {});
  expect(result.current).toBe(2);
  expect(result.results.map(r => r.title.raw)).toEqual(['c']);
  expect(result.pagingStart).toBe(3);
  expect(result.rawResponse.captureGroups.hasNext).toBe(false);
});

test('a failed superseded batch cannot surface an error over the newer results', async () => {
  let reject;
  const fetch = makeSearch([record('new')]);
  fetch.mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }));
  const search = createGroupedSearch(fetch, () => ({}));
  const old = search(state, {});
  await search({ ...state, searchTerm: 'new' }, {});
  reject(new Error('old request failed'));
  await expect(old).resolves.toBeDefined();
  expect((await search({ ...state, searchTerm: 'new' }, {})).results[0].title.raw).toBe('new');
});
