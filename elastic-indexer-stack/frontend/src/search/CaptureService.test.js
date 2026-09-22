import { fetchCaptures } from './CaptureService';
import { captureIdentity } from './CaptureIdentity';

const id = 'websites/example.org/20000101000000/a.b?id=1&from=(x)';
const source = { title: 'A capture', url: 'https://web.archive.org/web/20000101000000/http://example.org/a.b?id=1&from=(x)', capture_date: '2000-01-01' };
const identity = captureIdentity({ _meta: { id }, url: { raw: source.url } });
const hit = (suffix = '', fields = source) => ({ _id: id + suffix, _source: fields });
const reply = (hits = [hit()], total = hits.length, extra = {}) => ({ ok: true, json: async () => ({ hits: { total: { value: total }, hits }, ...extra }) });
beforeEach(() => { global.fetch = jest.fn().mockResolvedValue(reply()); });

test('loads metadata for exact page captures, with regex characters escaped and stable date ordering', async () => {
  const result = await fetchCaptures(identity);
  const [url, options] = fetch.mock.calls[0];
  expect(url).toBe('/elasticsearch/eq-archive/_search?preference=archive-captures');
  const request = JSON.parse(options.body);
  expect(request.query.bool.should[0]).toEqual({ regexp: { id: 'websites/example\\.org/[0-9]{14}/a\\.b\\?id=1\\&from=\\(x\\)' } });
  expect(request._source).not.toContain('text_full');
  expect(request._source).not.toContain('text');
  expect(request.sort[0]).toEqual({ capture_date: { order: 'desc', missing: '_last' } });
  expect(result.records[0]._meta.id).toBe(id);
  expect(result.nextOffset).toBeNull();
});

test('rejects approximate URL matches, unknown IDs, and child attachments from legacy candidates', async () => {
  fetch.mockResolvedValueOnce(reply([hit(), hit('other', { ...source, url: source.url.replace('id=1', 'id=2') }),
    hit('attachment', { ...source, parent_id: 'parent' }), { _source: source }, { _id: 'unknown' }]));
  expect((await fetchCaptures(identity)).records.map(r => r._meta.id)).toEqual([id]);
});

test('paginates metadata and labels the 1,000-capture limit', async () => {
  fetch.mockResolvedValue(reply(Array.from({ length: 50 }, (_, i) => hit(String(i))), 1001));
  expect((await fetchCaptures(identity)).nextOffset).toBe(50);
  expect((await fetchCaptures(identity, 950))).toMatchObject({ nextOffset: null, limited: true });
  expect(JSON.parse(fetch.mock.calls[1][1].body).from).toBe(950);
});

test.each([null, -1, 1000, 1.5])('rejects invalid requests before accessing the archive: %s', async offset => {
  await expect(offset === null ? fetchCaptures(null) : fetchCaptures(identity, offset)).rejects.toThrow('Invalid capture request');
  expect(fetch).not.toHaveBeenCalled();
});

test.each([
  { ok: false },
  reply([], 0, { timed_out: true }),
  reply([], 0, { _shards: { failed: 1 } }),
  { ok: true, json: async () => ({}) }
])('does not treat failed or partial responses as complete capture history', async response => {
  fetch.mockResolvedValue(response);
  await expect(fetchCaptures(identity)).rejects.toThrow();
});

test('passes cancellation to the request', async () => {
  const controller = new AbortController();
  await fetchCaptures(identity, 0, controller.signal);
  expect(fetch.mock.calls[0][1].signal).toBe(controller.signal);
});
