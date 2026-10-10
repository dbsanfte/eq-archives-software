import { getConfig } from '../config/config-helper';

export const RECENT_SITE_LIMIT = 50;

// Order by a descending maximum: Elasticsearch can find the newest domains
// across shards without sampling individual search results or fetching text.
export function recentSitesQuery() {
  return {
    size: 0, track_total_hits: false, timeout: '8s',
    query: { bool: { filter: [
      { prefix: { id: 'websites/' } },
      { exists: { field: 'last_indexed' } }
    ] } },
    aggs: { sites: {
      terms: { field: 'domain_name', size: RECENT_SITE_LIMIT,
        order: [{ latest_indexed: 'desc' }, { _key: 'asc' }] },
      aggs: {
        latest_indexed: { max: { field: 'last_indexed' } },
        first_capture: { min: { field: 'capture_date' } },
        last_capture: { max: { field: 'capture_date' } }
      }
    } }
  };
}

export function siteSearchUrl(domain) {
  const params = new URLSearchParams();
  params.set('filters[0][field]', 'domain_name');
  params.set('filters[0][values][0]', domain);
  params.set('filters[0][type]', 'any');
  return `/?${params}`;
}

function validTime(value) {
  return typeof value === 'number' && Number.isFinite(value) && !Number.isNaN(new Date(value).getTime());
}

export async function fetchRecentSites(signal) {
  const index = encodeURIComponent(getConfig().indexName);
  const response = await fetch(`/elasticsearch/${index}/_search`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
    body: JSON.stringify(recentSitesQuery())
  });
  if (!response.ok) throw new Error('Site request failed');
  const data = await response.json();
  const buckets = data.aggregations?.sites?.buckets;
  if (data.timed_out || data._shards?.failed || !Array.isArray(buckets) || buckets.some(bucket =>
    typeof bucket.key !== 'string' || !bucket.key.trim() ||
    !validTime(bucket.latest_indexed?.value) || !Number.isSafeInteger(bucket.doc_count) || bucket.doc_count < 1
  )) throw new Error('Site request incomplete');
  return buckets.map(bucket => ({
    domain: bucket.key,
    indexedAt: bucket.latest_indexed.value,
    records: bucket.doc_count,
    // Counts/ranges from metric-ordered terms can omit documents on other
    // shards. The latest timestamp/order is reliable; never claim exact counts.
    partial: data._shards?.total !== 1,
    firstCapture: validTime(bucket.first_capture?.value) ? bucket.first_capture.value : null,
    lastCapture: validTime(bucket.last_capture?.value) ? bucket.last_capture.value : null
  }));
}
