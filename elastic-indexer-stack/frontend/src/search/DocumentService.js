import { getConfig } from '../config/config-helper';

export const DOCUMENT_UNAVAILABLE = 'This record is no longer available.';

export async function fetchDocumentText(id, signal) {
  if (!id) throw new Error(DOCUMENT_UNAVAILABLE);
  const index = encodeURIComponent(getConfig().indexName || 'eq-archive');
  const response = await fetch(`/elasticsearch/${index}/_search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({ size: 1, _source: ['text_full'], query: { ids: { values: [id] } } })
  });
  if (!response.ok) throw new Error('Document request failed');
  const data = await response.json();
  const hit = data.hits?.hits?.[0];
  if (!hit || hit._id !== id) throw new Error(DOCUMENT_UNAVAILABLE);
  return typeof hit._source?.text_full === 'string' ? hit._source.text_full : '';
}

// The reader retrieves one exact source and its provenance, never vectors,
// nested chunks or generated summaries. Search results stay lightweight.
export const READER_FIELDS = [
  'title', 'url', 'alternate_url', 'capture_date', 'llm_guessed_date',
  'domain_name', 'mailing_list_name', 'file_type', 'mime_type', 'parent_id',
  'text_full', 'llm_image_text_full'
];

export async function fetchDocumentRecord(id, signal) {
  if (!id) throw new Error(DOCUMENT_UNAVAILABLE);
  const index = encodeURIComponent(getConfig().indexName || 'eq-archive');
  const response = await fetch(`/elasticsearch/${index}/_search`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
    body: JSON.stringify({ size: 1, _source: READER_FIELDS, query: { ids: { values: [id] } } })
  });
  if (!response.ok) throw new Error('Document request failed');
  const data = await response.json();
  if (data.timed_out || data._shards?.failed) throw new Error('Document request incomplete');
  const hit = data.hits?.hits?.[0];
  if (!hit || hit._id !== id) throw new Error(DOCUMENT_UNAVAILABLE);
  return {
    ...Object.fromEntries(READER_FIELDS.filter(field => hit._source?.[field] != null).map(field => [field, { raw: hit._source[field] }])),
    _meta: { id: hit._id }, id: { raw: hit._id }
  };
}
