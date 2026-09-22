class EmbeddingService {
  constructor() {
    this.cache = new Map();
    this.pendingQueries = new Map();
    this.isServiceAvailable = true;
    this.lastFailureTime = null;
    this.RETRY_INTERVAL = 60000; // 60 seconds
    this.FETCH_TIMEOUT = 5000; // 5 seconds

    // Create notification container once
    this.createNotificationContainer();
  }

  async fetchEmbedding(query, model, { signal } = {}) {
    if (!query || signal?.aborted) return null;
    if (this.cache.has(query)) return this.cache.get(query);
    if (!this.isServiceAvailable && !this.shouldRetryService()) return null;

    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal?.addEventListener('abort', cancel, { once: true });
    let timedOut = false;
    const aborted = new Promise((_, reject) => {
      controller.signal.addEventListener('abort', () => reject(new Error('Embedding request cancelled')), { once: true });
    });
    const timeout = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, this.FETCH_TIMEOUT);

    try {
      // Nomic requires a task prefix; keep the cache keyed by the user query.
      const input = model?.includes('nomic-embed-text-v1.5')
        ? `search_query: ${query}` : query;
      const responseBody = fetch('/openai/v1/embeddings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({ input: [input], model })
      }).then(response => {
        if (!response.ok) throw new Error('Embedding request failed');
        return response.json();
      });
      // The deadline and cancellation cover the response body as well as headers.
      const data = await Promise.race([responseBody, aborted]);
      const vector = data?.data?.[0]?.embedding;
      if (!Array.isArray(vector) || !vector.length || !vector.every(Number.isFinite)) {
        throw new Error('Invalid embedding response');
      }
      if (!this.isServiceAvailable) {
        this.isServiceAvailable = true;
        this.showServiceRestoredNotification();
      }
      this.cache.set(query, vector);
      this.clearOldEmbeddings();
      return vector;
    } catch (error) {
      // Editing or leaving the page is not an upstream service failure.
      if (signal?.aborted && !timedOut) return null;
      console.error('Error fetching embedding:', error);
      if (this.isServiceAvailable) {
        this.isServiceAvailable = false;
        this.lastFailureTime = Date.now();
        this.showServiceUnavailableNotification();
      }
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener('abort', cancel);
    }
    return null;
  }

  getEmbedding(query) {
    return this.cache.get(query);
  }

  clearOldEmbeddings(maxSize = 50) {
    if (this.cache.size > maxSize) {
      const keysToDelete = [...this.cache.keys()].slice(0, this.cache.size - maxSize);
      keysToDelete.forEach(key => this.cache.delete(key));
    }
  }

  shouldRetryService() {
    // Check if cool-down period has passed
    if (this.lastFailureTime === null) return true;

    const elapsedTime = Date.now() - this.lastFailureTime;
    return elapsedTime > this.RETRY_INTERVAL;
  }

  createNotificationContainer() {
    // Check if container already exists
    if (document.getElementById('embedding-service-notifications')) return;

    const notificationContainer = document.createElement('div');
    notificationContainer.id = 'embedding-service-notifications';
    notificationContainer.style.position = 'fixed';
    notificationContainer.style.top = '0';
    notificationContainer.style.left = '0';
    notificationContainer.style.right = '0';
    notificationContainer.style.zIndex = '9999';
    notificationContainer.style.display = 'flex';
    notificationContainer.style.flexDirection = 'column';
    notificationContainer.style.alignItems = 'center';

    document.body.appendChild(notificationContainer);
  }

  showNotification(message, type = 'warning', duration = 5000) {
    const container = document.getElementById('embedding-service-notifications');
    if (!container) return;

    const notification = document.createElement('div');
    notification.textContent = message;
    notification.style.margin = '10px';
    notification.style.padding = '10px 20px';
    notification.style.borderRadius = '4px';
    notification.style.boxShadow = '0 2px 5px rgba(0,0,0,0.2)';
    notification.style.backgroundColor = type === 'warning' ? '#fff3cd' : '#d4edda';
    notification.style.color = type === 'warning' ? '#856404' : '#155724';
    notification.style.border = type === 'warning' ? '1px solid #ffeeba' : '1px solid #c3e6cb';

    container.appendChild(notification);

    // Auto-dismiss after duration
    setTimeout(() => {
      notification.style.opacity = '0';
      notification.style.transition = 'opacity 0.5s';
      setTimeout(() => container.removeChild(notification), 500);
    }, duration);
  }

  showServiceUnavailableNotification() {
    this.showNotification('Embedding service is unavailable. Semantic search has been temporarily disabled.', 'warning');
  }

  showServiceRestoredNotification() {
    this.showNotification('Embedding service has been restored. Semantic search is now available.', 'success');
  }

  isEmbeddingServiceAvailable() {
    return this.isServiceAvailable || this.shouldRetryService();
  }
}

export const embeddingService = new EmbeddingService();
