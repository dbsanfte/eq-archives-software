import { DOCUMENT_UNAVAILABLE, fetchDocumentRecord, READER_FIELDS } from './DocumentService';
const id = 'archive/exact ?#%ü';
const response = data => ({ ok: true, json: async () => data });
beforeEach(() => { global.fetch = jest.fn(); });

test('fetches one exact record with source text, labelled OCR and provenance only', async () => {
  fetch.mockResolvedValue(response({ hits: { hits: [{ _id: id, _source: { title: 'Title', text_full: 'Original', llm_image_text_full: 'OCR', llm_summary: 'Generated', text: [{ vector: [1] }], parent_id: null } }] } }));
  const controller = new AbortController();
  const record = await fetchDocumentRecord(id, controller.signal);
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ size: 1, _source: READER_FIELDS, query: { ids: { values: [id] } } });
  expect(fetch.mock.calls[0][1].signal).toBe(controller.signal);
  expect(record).toEqual({ _meta: { id }, id: { raw: id }, title: { raw: 'Title' }, text_full: { raw: 'Original' }, llm_image_text_full: { raw: 'OCR' } });
  expect(READER_FIELDS).not.toEqual(expect.arrayContaining(['llm_summary', 'text', 'llm_summary_vector']));
});

test.each([{}, { hits: { hits: [] } }, { hits: { hits: [{ _id: 'wrong' }] } }])('rejects missing or mismatched records: %p', async data => {
  fetch.mockResolvedValue(response(data));
  await expect(fetchDocumentRecord(id)).rejects.toThrow(DOCUMENT_UNAVAILABLE);
});

test.each([{ timed_out: true }, { _shards: { failed: 1 } }])('rejects partial retrievals: %p', async data => {
  fetch.mockResolvedValue(response(data));
  await expect(fetchDocumentRecord(id)).rejects.toThrow('incomplete');
});

test('does not send missing IDs or accept failed HTTP responses', async () => {
  await expect(fetchDocumentRecord('')).rejects.toThrow(DOCUMENT_UNAVAILABLE);
  expect(fetch).not.toHaveBeenCalled();
  fetch.mockResolvedValue({ ok: false });
  await expect(fetchDocumentRecord(id)).rejects.toThrow('request failed');
});

test('supports metadata-only records without manufacturing text', async () => {
  fetch.mockResolvedValue(response({ hits: { hits: [{ _id: id }] } }));
  expect(await fetchDocumentRecord(id)).toEqual({ _meta: { id }, id: { raw: id } });
});
