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
