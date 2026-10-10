import { fetchRecentSites, recentSitesQuery, siteSearchUrl } from './RecentSitesService';
import { getConfig } from '../config/config-helper';

jest.mock('../config/config-helper', () => ({ getConfig: jest.fn(() => ({ indexName: 'eq-archive' })) }));
const bucket = (changes = {}) => ({ key: 'eq.example.org', doc_count: 123,
  latest_indexed: { value: Date.UTC(2026, 9, 10) },
  first_capture: { value: Date.UTC(1999, 0, 1) }, last_capture: { value: Date.UTC(2006, 0, 1) }, ...changes });
const reply = (buckets = [bucket()], changes = {}) => ({
  _shards: { total: 1, failed: 0 }, aggregations: { sites: { buckets } }, ...changes
});

beforeEach(() => {
  global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => reply() });
  getConfig.mockReturnValue({ indexName: 'eq-archive' });
});

test('requests only bounded website aggregations, ordered by actual indexing date, through the public proxy', async () => {
  const controller = new AbortController();
  getConfig.mockReturnValue({ indexName: 'index/alias' });
  expect(await fetchRecentSites(controller.signal)).toEqual([{
    domain: 'eq.example.org', records: 123, indexedAt: Date.UTC(2026, 9, 10),
    firstCapture: Date.UTC(1999, 0, 1), lastCapture: Date.UTC(2006, 0, 1), partial: false
  }]);
  const [url, options] = fetch.mock.calls[0];
  expect(url).toBe('/elasticsearch/index%2Falias/_search');
  expect(options.signal).toBe(controller.signal);
  const body = JSON.parse(options.body);
  expect(body).toEqual(recentSitesQuery());
  expect(body).toMatchObject({ size: 0, track_total_hits: false, timeout: '8s',
    query: { bool: { filter: [{ prefix: { id: 'websites/' } }, { exists: { field: 'last_indexed' } }] } },
    aggs: { sites: { terms: { field: 'domain_name', size: 50, order: [{ latest_indexed: 'desc' }, { _key: 'asc' }] } } }
  });
  expect(body._source).toBeUndefined();
  expect(body.knn).toBeUndefined();
});

test.each([
  { timed_out: true }, { _shards: { failed: 1 } }, {},
  { aggregations: { sites: { buckets: null } } },
  reply([bucket({ key: '' })]), reply([bucket({ key: null })]),
  reply([bucket({ latest_indexed: {} })]), reply([bucket({ latest_indexed: { value: 'today' } })]),
  reply([bucket({ latest_indexed: { value: Infinity } })]), reply([bucket({ latest_indexed: { value: 1e20 } })]),
  reply([bucket({ doc_count: -1 })]), reply([bucket({ doc_count: 1.5 })]),
])('rejects incomplete or invalid lists instead of silently dropping sites: %j', async data => {
  fetch.mockResolvedValue({ ok: true, json: async () => data });
  await expect(fetchRecentSites()).rejects.toThrow('incomplete');
});

test('surfaces HTTP and connection failures, including cancellation', async () => {
  fetch.mockResolvedValueOnce({ ok: false });
  await expect(fetchRecentSites()).rejects.toThrow('failed');
  fetch.mockRejectedValueOnce(new DOMException('Aborted', 'AbortError'));
  await expect(fetchRecentSites()).rejects.toMatchObject({ name: 'AbortError' });
});

test('handles empty results and missing dates without inventing values', async () => {
  fetch.mockResolvedValueOnce({ ok: true, json: async () => reply([]) });
  expect(await fetchRecentSites()).toEqual([]);
  fetch.mockResolvedValueOnce({ ok: true, json: async () => reply([bucket({ first_capture: { value: null }, last_capture: {} })], { _shards: { total: 2 } }) });
  expect(await fetchRecentSites()).toEqual([expect.objectContaining({ firstCapture: null, lastCapture: null, partial: true })]);
});

test('encodes an exact facet rather than injecting the domain into search syntax or an external link', () => {
  const domain = 'www.example.org:8080 & q=cleric " OR *';
  const url = new URL(siteSearchUrl(domain), 'https://search.eqarchives.org');
  expect(url.origin + url.pathname).toBe('https://search.eqarchives.org/');
  expect([...url.searchParams.entries()]).toEqual([
    ['filters[0][field]', 'domain_name'], ['filters[0][values][0]', domain], ['filters[0][type]', 'any']
  ]);
});
