// Be conservative: only timestamped website captures share an identity. Messages,
// attachments and unknown records keep their exact Elasticsearch identity.
export function captureIdentity(result) {
  if (result.parent_id?.raw) return null;
  const id = result._meta?.id || result.id?.raw;
  const path = typeof id === 'string' && id.match(/^websites\/([^/]+)\/\d{14}\/(.*)$/);
  const replay = typeof result.url?.raw === 'string' && result.url.raw.match(
    /^https?:\/\/web\.archive\.org\/web\/\d{14}(?:[a-z]+_)?\/(https?:\/\/.+)$/i
  );
  if (!path && !replay) return null;
  try {
    const url = new URL(replay ? replay[1] : `http://${path[1]}/${path[2]}`);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    url.hash = '';
    return {
      key: `page:${url.href}`,
      originalUrl: url.href,
      // Archive IDs preserve the stored path and query string verbatim.
      archiveHost: path ? path[1] : url.host,
      archivePath: path ? path[2] : `${url.pathname.slice(1)}${url.search}`
    };
  } catch (_) {
    return null;
  }
}

export function captureKey(result, fallback) {
  return captureIdentity(result)?.key || `record:${result._meta?.id || result.id?.raw || fallback}`;
}
