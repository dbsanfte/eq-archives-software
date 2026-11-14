import { createFusedSearch, createPreparedSearch, fuseResults } from './FusedSearch';

const result = id => ({ _meta: { id }, title: { raw: id } });
const state = { searchTerm: 'bard songs', current: 1, resultsPerPage: 20 };
const context = () => ({ params: { enableSemanticSearch: true }, embedding: [1] });
const ids = response => response.results.map(row => row._meta.id);

test('bookmarked searches prepare embeddings before grouping context is captured', async () => {
  let embedding;
  const getContext = () => ({ ...context(), embedding });
  const search = jest.fn(async () => ({ embedding }));
  const prepare = jest.fn(async () => { embedding = [1]; });
  const prepared = createPreparedSearch(search, getContext, prepare);
  expect(await prepared(state, {})).toEqual({ embedding: [1] });
  await prepared(state, {});
  expect(prepare).toHaveBeenCalledTimes(1);
});

test('late embedding preparation cannot run an obsolete search or overwrite a newer cache', async () => {
  let finish;
  const prepare = jest.fn(() => new Promise(resolve => { finish = resolve; }));
  const search = jest.fn(async request => ({ results: [result(request.searchTerm)] }));
  const prepared = createPreparedSearch(search, () => ({ ...context(), embedding: null }), prepare);
  const old = prepared(state, {});
  const signal = prepare.mock.calls[0][1].signal;
  const next = { ...state, searchTerm: '"cleric"' };
  expect(ids(await prepared(next, {}))).toEqual(['"cleric"']);
  expect(signal.aborted).toBe(true);
  finish();
  expect(await old).toEqual({ results: [], totalResults: 0 });
  expect(search).toHaveBeenCalledTimes(1);
});

test('embedding failure falls back without turning the query into an error', async () => {
  const search = jest.fn(async () => ({ results: [], totalResults: 0 }));
  const prepared = createPreparedSearch(search, () => ({ ...context(), embedding: null }), async () => { throw new Error('offline'); });
  expect(await prepared(state, {})).toEqual({ results: [], totalResults: 0 });
  expect(search).toHaveBeenCalledWith(state, {});
});

test('failed embedding preparation stays fixed across pages, and scope changes permit a retry', async () => {
  const search = jest.fn(async () => ({ results: [], totalResults: 0 }));
  const prepare = jest.fn(async () => null);
  const prepared = createPreparedSearch(search, () => ({ ...context(), embedding: null }), prepare);
  await prepared(state, {});
  await prepared({ ...state, current: 2 }, {});
  expect(prepare).toHaveBeenCalledTimes(1);
  await prepared({ ...state, filters: [{ field: 'domain_name', values: ['example.org'] }] }, {});
  expect(prepare).toHaveBeenCalledTimes(2);
});

test('concurrent pages share embedding preparation and only the latest enters grouping', async () => {
  let finish;
  const prepare = jest.fn(() => new Promise(resolve => { finish = resolve; }));
  const search = jest.fn(async request => ({ current: request.current }));
  const prepared = createPreparedSearch(search, () => ({ ...context(), embedding: null }), prepare);
  const a = prepared(state, {}), b = prepared({ ...state, current: 2 }, {});
  finish();
  expect(await a).toEqual({ results: [], totalResults: 0 });
  expect(await b).toEqual({ current: 2 });
  expect(prepare).toHaveBeenCalledTimes(1);
  expect(search).toHaveBeenCalledTimes(1);
});

function fixture(count = 250, semantic = [result('outside'), result('120'), result('1')]) {
  const lexical = Array.from({ length: count }, (_, n) => result(String(n)));
  const search = jest.fn(async request => {
    const rows = request._archiveRetrieval === 'semantic' ? semantic : lexical;
    const start = (request.current - 1) * request.resultsPerPage;
    return { results: rows.slice(start, start + request.resultsPerPage), totalResults: rows.length, facets: { domain: 'retained' } };
  });
  return { search, fused: createFusedSearch(search, context), lexical, semantic };
}

test('RRF combines ranks once per ID, clips each head and orders Unicode ties deterministically', () => {
  expect(fuseResults([result('a'), result('b')], [result('b'), result('c')]).map(row => row._meta.id)).toEqual(['b', 'a', 'c']);
  expect(fuseResults([result('a'), result('a')], []).map(row => row._meta.id)).toEqual(['a']);
  expect(fuseResults([result('aa'), result('a')], [result('a'), result('aa')]).map(row => row._meta.id)).toEqual(['a', 'aa']);
  expect(fuseResults([result('😀'), result('\ue000')], [result('\ue000'), result('😀')]).map(row => row._meta.id)).toEqual(['\ue000', '😀']);
  expect(fuseResults(Array.from({ length: 101 }, (_, n) => result(String(n))), [])).toHaveLength(100);
});

test('fixed fusion head precedes a deduplicated lexical tail with stable back navigation', async () => {
  const { fused, search, lexical, semantic } = fixture();
  const first = await fused(state, {});
  expect(ids(first)).toEqual(fuseResults(lexical.slice(0, 100), semantic).slice(0, 20).map(row => row._meta.id));
  const all = [];
  for (let current = 1; current <= 13; current++) all.push(...ids(await fused({ ...state, current }, {})));
  expect(new Set(all).size).toBe(251);
  expect(all).toHaveLength(251);
  expect(all).toContain('249');
  expect(first.facets).toEqual({ domain: 'retained' });
  const calls = search.mock.calls.length;
  expect(ids(await fused(state, {}))).toEqual(ids(first));
  expect(search).toHaveBeenCalledTimes(calls);
  expect(search.mock.calls.filter(([s]) => s._archiveRetrieval === 'semantic')).toHaveLength(1);
});

test('concurrent pages share head and tail requests', async () => {
  const { fused, search } = fixture();
  const [a, b] = await Promise.all([fused({ ...state, current: 8 }, {}), fused({ ...state, current: 9 }, {})]);
  expect(ids(a).some(id => ids(b).includes(id))).toBe(false);
  const calls = search.mock.calls.map(([s]) => `${s._archiveRetrieval}:${s.current}:${s.resultsPerPage}`);
  expect(new Set(calls).size).toBe(calls.length);
});

test('semantic failure remains a lexical fallback for the whole paging session', async () => {
  const { search } = fixture();
  const fetch = jest.fn(request => request._archiveRetrieval === 'semantic' ? Promise.reject(new Error('busy')) : search(request));
  const fused = createFusedSearch(fetch, context);
  expect(ids(await fused(state, {}))).toEqual(Array.from({ length: 20 }, (_, n) => String(n)));
  await fused({ ...state, current: 8 }, {});
  await fused(state, {});
  expect(fetch.mock.calls.filter(([s]) => s._archiveRetrieval === 'semantic')).toHaveLength(1);
});

test('empty lexical results still return vector matches and a finite total', async () => {
  const { fused } = fixture(0, [result('vector')]);
  const response = await fused(state, {});
  expect(ids(response)).toEqual(['vector']);
  expect(response.totalResults).toBe(1);
});

test('metadata failure keeps usable keyword facets and a stable result head', async () => {
  const { search } = fixture(30);
  const fetch = jest.fn(request => request._archiveRetrieval === 'metadata' ? Promise.reject(new Error('busy')) : search(request));
  const fused = createFusedSearch(fetch, context);
  const response = await fused(state, {});
  expect(response.facets).toEqual({ domain: 'retained' });
  expect(await fused(state, {})).toEqual(response);
  expect(fetch.mock.calls.filter(([s]) => s._archiveRetrieval === 'metadata')).toHaveLength(1);
});

test.each([
  [{ ...state, sortField: 'capture_date' }, context()],
  [{ ...state, searchTerm: '"bard" -cleric' }, context()],
  [state, { ...context(), embedding: null }],
  [state, { ...context(), params: { enableSemanticSearch: false } }]
])('date order, constraints and unavailable/disabled embeddings bypass fusion', async (request, settings) => {
  const search = jest.fn(async () => ({ results: [], totalResults: 0 }));
  await createFusedSearch(search, () => settings)(request, {});
  expect(search).toHaveBeenCalledWith(request, {});
  expect(search).toHaveBeenCalledTimes(1);
});

test('scope changes invalidate the head and lexical failures can be retried', async () => {
  const { search } = fixture(30);
  search.mockRejectedValueOnce(new Error('temporary'));
  const fused = createFusedSearch(search, context);
  await expect(fused(state, {})).rejects.toThrow('temporary');
  expect((await fused(state, {})).results).toHaveLength(20);
  await fused({ ...state, filters: [{ field: 'domain_name', values: ['example.org'] }] }, {});
  expect(search.mock.calls.filter(([s]) => s._archiveRetrieval === 'semantic')).toHaveLength(2);
});

test('obsolete failures cannot discard a newer successful search', async () => {
  let reject;
  const { search } = fixture(30);
  search.mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }));
  const fused = createFusedSearch(search, context);
  const old = fused(state, {});
  const newest = { ...state, searchTerm: 'cleric healing' };
  const response = await fused(newest, {});
  reject(new Error('obsolete'));
  await expect(old).rejects.toThrow('obsolete');
  const count = search.mock.calls.length;
  expect(await fused(newest, {})).toEqual(response);
  expect(search).toHaveBeenCalledTimes(count);
});

test('the raw browsing window and tail work stay bounded at 1000', async () => {
  const { fused, search } = fixture(2000);
  const response = await fused({ ...state, current: 50 }, {});
  expect(response.results).toHaveLength(20);
  expect(response.totalResults).toBe(1001);
  expect((await fused({ ...state, current: 51 }, {})).results).toHaveLength(0);
  expect(search.mock.calls.filter(([s]) => s._archiveRetrieval === 'lexical').every(([s]) => (s.current - 1) * s.resultsPerPage < 1000)).toBe(true);
});
