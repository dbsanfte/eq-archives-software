import { cacheReaderReturn, forgetReaderReturn, readReaderReturn, searchPageKey } from './ReaderReturnCache';
import { readerUrl } from '../views/reader/reader-utils';

const url = '/?q=cleric&current=n_2_n';
const state = { searchTerm: 'cleric', current: 2 };
const request = searchPageKey(state);
const result = { title: { raw: 'A capture' } };
const display = { results: [result], totalResults: 80, totalPages: 4, current: 2, pagingStart: 21,
  pagingEnd: 40, requestId: 'fixture', facets: { domain_name: [] }, rawResponse: {
    captureGroups: { hasNext: true }, hits: ['Raw search response is not needed']
  } };
const snapshot = { phase: 'idle', request, display };
const parameters = { enableSemanticSearch: false, groupCaptures: true };

function readerClick(target = readerUrl('archive/capture', { returnTo: url })) {
  const anchor = document.createElement('a');
  const label = document.createElement('span');
  anchor.href = target;
  anchor.append(label);
  return { button: 0, target: label };
}

beforeEach(() => {
  window.history.replaceState(null, '', url);
  window.sessionStorage.clear();
});
afterEach(() => {
  window.history.replaceState(null, '', '/');
  jest.restoreAllMocks();
});

test('holds the final page and facet data for the exact return URL', () => {
  cacheReaderReturn(readerClick(), snapshot, parameters);
  expect(readReaderReturn()).toMatchObject({ url, request, parameters, response: {
    ...display, rawResponse: { captureGroups: { hasNext: true } }
  } });
  expect(readReaderReturn().response.rawResponse.hits).toBeUndefined();
  window.history.replaceState(null, '', '/?q=cleric&current=n_3_n');
  expect(readReaderReturn()).toBeNull();
  forgetReaderReturn();
  window.history.replaceState(null, '', url);
  expect(readReaderReturn()).toBeNull();
});

test('a plain reader click with a finished search is the only action that stores results', () => {
  const click = readerClick();
  for (const flags of [{ defaultPrevented: true }, { button: 1 }, { ctrlKey: true }, { metaKey: true },
    { shiftKey: true }, { altKey: true }]) cacheReaderReturn({ ...click, ...flags }, snapshot, parameters);
  cacheReaderReturn(readerClick('https://example.org/document'), snapshot, parameters);
  cacheReaderReturn(readerClick('/document?id=other'), snapshot, parameters);
  cacheReaderReturn({ button: 0, target: document.createElement('button') }, snapshot, parameters);
  click.target.parentElement.target = '_blank';
  cacheReaderReturn(click, snapshot, parameters);
  click.target.parentElement.target = '_self';
  click.target.parentElement.download = 'report';
  cacheReaderReturn(click, snapshot, parameters);
  cacheReaderReturn(readerClick(), { ...snapshot, phase: 'waiting' }, parameters);
  cacheReaderReturn(readerClick(), { ...snapshot, display: { results: [] } }, parameters);
  cacheReaderReturn(readerClick(), { ...snapshot, request: undefined }, parameters);
  expect(readReaderReturn()).toBeNull();
});

test('expired, oversized and unavailable storage fall back cleanly', () => {
  const now = jest.spyOn(Date, 'now').mockReturnValue(5000);
  cacheReaderReturn(readerClick(), snapshot, parameters);
  now.mockReturnValue(5000 + 60 * 60 * 1000);
  expect(readReaderReturn()).toBeNull();
  cacheReaderReturn(readerClick(), { ...snapshot, display: { results: ['x'.repeat(2 * 1024 * 1024)] } }, parameters);
  expect(readReaderReturn()).toBeNull();
  const broken = jest.spyOn(Storage.prototype, 'getItem').mockReturnValueOnce('{');
  expect(readReaderReturn()).toBeNull();
  broken.mockImplementation(() => { throw new Error('Storage unavailable'); });
  expect(readReaderReturn()).toBeNull();
  jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Quota exceeded'); });
  jest.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('Storage unavailable'); });
  expect(() => cacheReaderReturn(readerClick(), snapshot, parameters)).not.toThrow();
});

test('search keys retain page, filters, size and effective sort across the URL round trip', () => {
  const filters = [{ field: 'capture_date', type: 'range', values: [{ from: '2000-01-01', to: '2001-01-01' }] }];
  const request = { ...state, filters, sortField: 'capture_date', sortDirection: 'desc' };
  const key = searchPageKey(request);
  expect(searchPageKey({ ...state, filters: [{ type: 'range', field: 'capture_date', values: [{ to: '2001-01-01', from: '2000-01-01' }] }],
    sortList: [{ field: 'capture_date', direction: 'desc' }] })).toBe(key);
  for (const changed of [{ searchTerm: 'wizard' }, { filters: [] }, { current: 3 }, { resultsPerPage: 50 },
    { sortDirection: 'asc' }]) expect(searchPageKey({ ...request, ...changed })).not.toBe(key);
});
