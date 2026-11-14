import { rerankResults, relevanceSearch, RERANK_WINDOW } from './Reranking';
import { createGroupedSearch } from './GroupedSearch';

const rows = Array.from({ length: 85 }, (_, n) => ({ _meta: { id: `newsgroups/${n}` }, title: { raw: String(n) } }));
const searchState = { searchTerm: 'bard', current: 1, resultsPerPage: 10 };
const search = jest.fn(async s => ({ results: rows.slice((s.current-1)*s.resultsPerPage, s.current*s.resultsPerPage), totalResults: rows.length }));
const reverse = jest.fn(async (_, results) => [...results].reverse());
afterEach(() => { jest.clearAllMocks(); delete global.fetch; jest.useRealTimers(); });

test('only relevance searches are reranked', () => {
  expect(relevanceSearch({})).toBe(false);
  expect(relevanceSearch(searchState)).toBe(true);
  expect(relevanceSearch({ ...searchState, sortField: 'capture_date' })).toBe(false);
  expect(relevanceSearch({ ...searchState, sortList: [{ field: '_score', direction: 'asc' }] })).toBe(false);
  expect(relevanceSearch({ ...searchState, sortList: [{ field: '_score', direction: 'desc' }] })).toBe(true);
});

test.each([true, false])('one bounded head permutation precedes paging, grouping=%s', async enabled => {
  const grouped = createGroupedSearch(search, () => ({ enabled }), reverse);
  const first = await grouped(searchState, {});
  const second = await grouped({ ...searchState, current: 2 }, {});
  const third = await grouped({ ...searchState, current: 3 }, {});
  expect(first.results.map(r => r.title.raw)).toEqual(Array.from({length:10}, (_,n) => String(RERANK_WINDOW-1-n)));
  expect(second.results.map(r => r.title.raw)).toEqual(Array.from({length:10}, (_,n) => String(RERANK_WINDOW-11-n)));
  expect(third.results.map(r => r.title.raw)).toEqual(Array.from({length:10}, (_,n) => String(RERANK_WINDOW-21-n)));
  const tail = await grouped({...searchState,current:RERANK_WINDOW/10+1},{});
  expect(tail.results).toEqual(rows.slice(RERANK_WINDOW,RERANK_WINDOW+10));
  expect(first).toMatchObject({pagingStart:1,pagingEnd:10});
  expect(second).toMatchObject({pagingStart:11,pagingEnd:20});
  expect(reverse).toHaveBeenCalledTimes(1);
  expect(reverse.mock.calls[0][1]).toHaveLength(RERANK_WINDOW);
  expect((await grouped(searchState, {})).results).toEqual(first.results);
  await grouped({ ...searchState, filters: [{ field:'domain_name', values:['x'] }] }, {});
  expect(reverse).toHaveBeenCalledTimes(2);
  await grouped({ ...searchState, sortField: 'capture_date', sortDirection:'asc' }, {});
  expect(reverse).toHaveBeenCalledTimes(2);
});

test('duplicates are grouped before ranking and dated representative is preserved', async () => {
  const item = n => ({_meta:{id:`websites/example.org/2000010100000${n}/index.html`},title:{raw:'same'}});
  const values = [item(0),item(1),...rows.slice(0,RERANK_WINDOW-1)];
  const grouped = createGroupedSearch(async () => ({results:values,totalResults:values.length}), () => ({}), reverse);
  const response = await grouped({...searchState,resultsPerPage:RERANK_WINDOW},{});
  expect(reverse.mock.calls[0][1]).toHaveLength(RERANK_WINDOW);
  expect(response.results[RERANK_WINDOW-1]).toEqual(item(0));
});

test('valid response only reorders exact original result objects', async () => {
  global.fetch = jest.fn(async () => ({ ok: true, json: async () => ({ ids:['newsgroups/1','newsgroups/0'] }) }));
  expect(await rerankResults('bard', rows.slice(0,2), 'eq-archive')).toEqual([rows[1],rows[0]]);
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({query:'bard',ids:['newsgroups/0','newsgroups/1']});
  expect(fetch.mock.calls[0][0]).toBe('/elasticsearch/eq-archive/_rerank');
});

test.each([{}, {ids:['other','newsgroups/0']}, {ids:['newsgroups/0','newsgroups/0']}, {ids:[]}])('invalid scores preserve baseline %j', async data => {
  global.fetch = jest.fn(async () => ({ok:true,json:async()=>data}));
  expect(await rerankResults('bard',rows.slice(0,2),'eq-archive')).toEqual(rows.slice(0,2));
});

test('errors, unsupported IDs and non-success status preserve results', async () => {
  global.fetch=jest.fn(async()=>({ok:false}));
  expect(await rerankResults('bard',rows.slice(0,2),'eq-archive')).toEqual(rows.slice(0,2));
  global.fetch.mockRejectedValue(new Error('offline'));
  expect(await rerankResults('bard',rows.slice(0,2),'eq-archive')).toEqual(rows.slice(0,2));
  for (const input of [[],[{}],[{},{}],[rows[0],rows[0]]]) expect(await rerankResults('bard',input,'eq-archive')).toEqual(input);
});

test('a stalled request is aborted and falls back', async () => {
  jest.useFakeTimers();
  global.fetch=jest.fn((_,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new Error('aborted')))));
  const flight=rerankResults('bard',rows.slice(0,2),'eq-archive');
  jest.advanceTimersByTime(4500);
  expect(await flight).toEqual(rows.slice(0,2));
});

test('delayed old ranking cannot contaminate a newer query or duplicate a concurrent ranking', async () => {
  let release;
  const rank=jest.fn((q,values)=>q==='old'?new Promise(resolve=>{release=()=>resolve([...values].reverse());}):Promise.resolve(values));
  const grouped=createGroupedSearch(search,()=>({}),rank);
  const old=grouped({...searchState,searchTerm:'old'},{});
  await Promise.resolve(); await Promise.resolve();
  const current=await grouped({...searchState,searchTerm:'new'},{});
  release(); await old;
  expect((await grouped({...searchState,searchTerm:'new'},{})).results).toEqual(current.results);
  expect(rank).toHaveBeenCalledTimes(2);
});

test('ungrouped deep bookmarks clamp to the available bounded window', async () => {
  const grouped = createGroupedSearch(search, () => ({enabled:false}), reverse);
  const response = await grouped({...searchState,current:50},{});
  expect(response.current).toBe(9);
  expect(response.results).toEqual(rows.slice(80));
  expect(response.pagingEnd).toBe(85);
});
