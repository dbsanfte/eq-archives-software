import { getConfig } from '../config/config-helper';
import { captureIdentity } from './CaptureIdentity';
import { CAPTURE_WINDOW } from './GroupedSearch';

const PAGE_SIZE = 50;
// Elasticsearch uses Lucene regexes, including optional operators such as @ and &.
const literal = value => value.replace(/[.?+*|{}[\]()"\\#@&<>~]/g, '\\$&');

export async function fetchCaptures(identity, offset = 0, signal) {
  if (!identity || !Number.isInteger(offset) || offset < 0 || offset >= CAPTURE_WINDOW) {
    throw new Error('Invalid capture request');
  }
  const index = encodeURIComponent(getConfig().indexName || 'eq-archive');
  const response = await fetch(`/elasticsearch/${index}/_search?preference=archive-captures`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
    body: JSON.stringify({
      from: offset, size: PAGE_SIZE, track_total_hits: CAPTURE_WINDOW + 1, timeout: '8s',
      _source: ['title', 'url', 'alternate_url', 'capture_date', 'parent_id'],
      sort: [{ capture_date: { order: 'desc', missing: '_last' } }, { id: { order: 'asc', missing: '_last' } }, { _doc: 'asc' }],
      query: { bool: { minimum_should_match: 1, should: [
        { regexp: { id: `websites/${literal(identity.archiveHost)}/[0-9]{14}/${literal(identity.archivePath)}` } },
        // Some older records lack a source id. Their URL is analyzed text, so
        // verify the exact original URL again below before showing any record.
        { bool: { must_not: [{ exists: { field: 'id' } }], must: [{ match_phrase: { url: identity.originalUrl } }] } }
      ] } }
    })
  });
  if (!response.ok) throw new Error('Capture request failed');
  const data = await response.json();
  if (data.timed_out || data._shards?.failed || !Array.isArray(data.hits?.hits)) throw new Error('Incomplete capture response');
  const hits = data.hits.hits;
  const records = hits.map(hit => ({
    ...Object.fromEntries(Object.entries(hit._source || {}).map(([field, raw]) => [field, { raw }])),
    _meta: { id: hit._id }, id: { raw: hit._id }
  })).filter(result => result._meta.id && captureIdentity(result)?.key === identity.key);
  const next = offset + hits.length;
  const total = data.hits.total.value;
  return {
    records,
    nextOffset: hits.length === PAGE_SIZE && next < total && next < CAPTURE_WINDOW ? next : null,
    limited: next >= CAPTURE_WINDOW && total > CAPTURE_WINDOW
  };
}
