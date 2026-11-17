// Searchkit does not expose its fetch transport through the connector. Install
// this adapter once at startup, scoped strictly to our same-origin read APIs.
// Other fetches, including third-party URLs, retain their original behavior.
export const BUSY_MESSAGE = 'Search is busy. Please try again in a few seconds.';
export const TIMEOUT_MESSAGE = 'The search service took too long to respond. Please try again.';
export const UNAVAILABLE_MESSAGE = 'The search service is temporarily unavailable. Please try again.';
export const INVALID_MESSAGE = 'This search could not be accepted. Try a shorter query or fewer filters.';

export class ArchiveRequestError extends Error {
  constructor(status, code) {
    super(code === 'busy' ? BUSY_MESSAGE : code === 'timeout' ? TIMEOUT_MESSAGE :
      status === 400 ? INVALID_MESSAGE : UNAVAILABLE_MESSAGE);
    this.status = status;
    this.code = code;
  }
}

export function createArchiveFetch(fetcher, { origin = window.location.origin, timeout = 12000 } = {}) {
  return async (input, options = {}) => {
    const url = new URL(input.url || input, origin);
    if (url.origin !== origin || !/^\/(?:elasticsearch\/[^/]+\/_(?:search|rerank|count)|openai\/v1\/embeddings|api\/explore\/(?:overview|phrases))$/.test(url.pathname)) {
      return fetcher(input, options);
    }
    const controller = new AbortController();
    const parent = options.signal || input.signal;
    const cancel = () => controller.abort();
    if (parent?.aborted) throw new DOMException('Request cancelled', 'AbortError');
    parent?.addEventListener('abort', cancel, { once: true });
    let expired = false;
    const deadline = Date.now() + timeout;
    const interrupted = new Promise((_, reject) => {
      controller.signal.addEventListener('abort', () => reject(expired
        ? new ArchiveRequestError(0, 'timeout') : new DOMException('Request cancelled', 'AbortError')), { once: true });
    });
    const timer = setTimeout(() => { expired = true; controller.abort(); }, timeout);
    const bounded = promise => Promise.race([promise, interrupted]);
    try {
      for (let attempt = 0; ; attempt++) {
        const request = typeof input.clone === 'function' ? input.clone() : input;
        const response = await bounded(fetcher(request, { ...options, signal: controller.signal }));
        // Drain through the deadline, including bodies which stall after headers.
        const body = await bounded(response.text());
        if (response.ok) return new Response(body, { status: response.status, headers: response.headers });
        let code;
        try { code = JSON.parse(body).error?.code; } catch (_) { /* Never display upstream bodies. */ }
        const transient = [429, 502, 503, 504].includes(response.status);
        const error = new ArchiveRequestError(response.status,
          code === 'timeout' ? 'timeout' : response.status === 429 || code === 'busy' ? 'busy' : 'unavailable');
        const retry = response.headers.get('Retry-After');
        const delay = retry === null ? 300 : /^\d+$/.test(retry) ? Number(retry) * 1000 : Date.parse(retry) - Date.now();
        // Optional reranking falls back promptly; required reads get one retry.
        if (attempt || !transient || url.pathname.endsWith('/_rerank') ||
            !Number.isFinite(delay) || delay > 5000 || Date.now() + Math.max(0, delay) >= deadline) throw error;
        let waiting;
        try { await bounded(new Promise(resolve => { waiting = setTimeout(resolve, Math.max(0, delay)); })); }
        finally { clearTimeout(waiting); }
      }
    } catch (error) {
      if (expired) throw new ArchiveRequestError(0, 'timeout');
      if (parent?.aborted || error instanceof ArchiveRequestError) throw error;
      throw new ArchiveRequestError(0, 'unavailable');
    } finally {
      clearTimeout(timer);
      parent?.removeEventListener('abort', cancel);
    }
  };
}

export function installArchiveFetch() {
  window.fetch = createArchiveFetch(window.fetch.bind(window));
}
