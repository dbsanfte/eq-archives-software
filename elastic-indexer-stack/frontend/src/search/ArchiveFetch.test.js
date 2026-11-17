import { createArchiveFetch, installArchiveFetch, ArchiveRequestError, BUSY_MESSAGE, INVALID_MESSAGE, TIMEOUT_MESSAGE, UNAVAILABLE_MESSAGE } from './ArchiveFetch';

const path = '/elasticsearch/eq-archive/_search';
const reply = (status = 200, data = { hits: [] }, headers = {}) => ({
  ok: status < 400, status, text: jest.fn(async () => JSON.stringify(data)), headers: new Headers(headers)
});

test('leaves unrelated and external requests untouched', async () => {
  const fetcher = jest.fn(async () => 'untouched');
  const request = createArchiveFetch(fetcher);
  for (const url of ['/api/unrelated', 'https://elsewhere.example/elasticsearch/eq-archive/_search']) {
    expect(await request(url, { method: 'POST' })).toBe('untouched');
    expect(fetcher).toHaveBeenLastCalledWith(url, { method: 'POST' });
  }
});

test('preserves successful JSON and headers and handles Request inputs', async () => {
  const fetcher = jest.fn(async () => reply(200, { count: 12 }, { 'X-Result': 'retained' }));
  const input = new Request(path, { method: 'POST', body: '{}' });
  const response = await createArchiveFetch(fetcher)(input);
  expect(await response.json()).toEqual({ count: 12 });
  expect(response.headers.get('X-Result')).toBe('retained');
  expect(fetcher.mock.calls[0][0]).not.toBe(input);
});

test.each([429, 502, 503, 504])('retries a temporary %s once and recovers', async status => {
  const fetcher = jest.fn().mockResolvedValueOnce(reply(status, { error: { code: 'busy' } }, { 'Retry-After': '0' }))
    .mockResolvedValueOnce(reply(200, { recovered: true }));
  expect(await (await createArchiveFetch(fetcher)(path, { method: 'POST', body: '{}' })).json()).toEqual({ recovered: true });
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(fetcher.mock.calls[1][1].body).toBe('{}');
});

test.each(['nonsense', '60'])('does not retry early for an unusable or excessive Retry-After %s', async value => {
  const fetcher = jest.fn(async () => reply(503, { error: { code: 'busy' } }, { 'Retry-After': value }));
  await expect(createArchiveFetch(fetcher)(path)).rejects.toMatchObject({ message: BUSY_MESSAGE, status: 503 });
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test('honors HTTP-date Retry-After and stops after the second rejection', async () => {
  const fetcher = jest.fn(async () => reply(429, {}, { 'Retry-After': new Date(Date.now() - 1000).toUTCString() }));
  await expect(createArchiveFetch(fetcher)(path)).rejects.toMatchObject({ code: 'busy' });
  expect(fetcher).toHaveBeenCalledTimes(2);
});

test('uses a bounded default retry delay, including safe errors for legacy responses', async () => {
  const fetcher = jest.fn(async () => reply(503, { error: { message: '<script>private upstream details</script>' } }));
  await expect(createArchiveFetch(fetcher)(path)).rejects.toThrow(UNAVAILABLE_MESSAGE);
  expect(fetcher).toHaveBeenCalledTimes(2);
});

test.each([400, 401, 403, 500])('does not retry permanent HTTP %s or display its response', async status => {
  const fetcher = jest.fn(async () => reply(status, 'secret upstream traceback'));
  await expect(createArchiveFetch(fetcher)(path)).rejects.toThrow(status === 400 ? INVALID_MESSAGE : UNAVAILABLE_MESSAGE);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test('optional reranking falls back without waiting out a busy interval', async () => {
  const fetcher = jest.fn(async () => reply(503, { error: { code: 'busy' } }, { 'Retry-After': '5' }));
  await expect(createArchiveFetch(fetcher)('/elasticsearch/eq-archive/_rerank')).rejects.toThrow(BUSY_MESSAGE);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test('sanitizes network failures and non-JSON errors', async () => {
  const fetcher = jest.fn().mockRejectedValueOnce(new Error('private connection detail')).mockResolvedValueOnce({
    ...reply(503), text: async () => '<html>private proxy diagnostic</html>'
  });
  const request = createArchiveFetch(fetcher);
  await expect(request(path)).rejects.toThrow(UNAVAILABLE_MESSAGE);
  await expect(request('/elasticsearch/eq-archive/_rerank')).rejects.toThrow(UNAVAILABLE_MESSAGE);
});

test('reports server timeouts without leaking details', async () => {
  const fetcher = jest.fn(async () => reply(503, { error: { code: 'timeout', message: 'private' } }, { 'Retry-After': '60' }));
  await expect(createArchiveFetch(fetcher)(path)).rejects.toThrow(TIMEOUT_MESSAGE);
});

test('the deadline covers a body stalled after successful headers', async () => {
  const fetcher = jest.fn(async () => ({ ...reply(), text: () => new Promise(() => {}) }));
  await expect(createArchiveFetch(fetcher, { timeout: 20 })(path)).rejects.toMatchObject({ code: 'timeout' });
  expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
});

test('does not schedule a retry beyond the request deadline', async () => {
  const fetcher = jest.fn(async () => reply(503, {}, { 'Retry-After': '5' }));
  await expect(createArchiveFetch(fetcher, { timeout: 20 })(path)).rejects.toBeInstanceOf(ArchiveRequestError);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test('cancellation stops retry waiting and an already cancelled request sends nothing', async () => {
  const controller = new AbortController();
  const fetcher = jest.fn(async () => reply(503, {}, { 'Retry-After': '5' }));
  const request = createArchiveFetch(fetcher)(path, { signal: controller.signal });
  await new Promise(resolve => setTimeout(resolve, 10));
  controller.abort();
  await expect(request).rejects.toMatchObject({ name: 'AbortError' });
  await expect(createArchiveFetch(fetcher)(path, { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' });
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test('startup installs the adapter without changing unrelated fetches', async () => {
  const before = window.fetch;
  window.fetch = jest.fn(async () => 'original');
  try {
    installArchiveFetch();
    expect(await window.fetch('/other')).toBe('original');
  } finally { window.fetch = before; }
});
