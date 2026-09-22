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

  async fetchEmbedding(query, model) {
    if (!query) return null;
    if (this.cache.has(query)) return this.cache.get(query);
    
    // Check if service is in cool-down period
    if (!this.isServiceAvailable && !this.shouldRetryService()) {
      console.warn("Embedding service is temporarily unavailable, skipping request");
      return null;
    }
    
    try {
      // Show that we're fetching this query
      console.log(`Fetching embedding for: ${query}`);
      
      // Create a promise that rejects after timeout
      const timeoutPromise = new Promise((_, reject) => {
        setTimeout(() => reject(new Error("Embedding request timed out")), this.FETCH_TIMEOUT);
      });
      
      // Nomic requires a task prefix; keep the cache keyed by the user query.
      const input = model?.includes('nomic-embed-text-v1.5')
        ? `search_query: ${query}`
        : query;

      // Create the actual fetch promise
      const fetchPromise = fetch("/openai/v1/embeddings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: [input], model })
      });
      
      // Race between fetch and timeout
      const response = await Promise.race([fetchPromise, timeoutPromise]);
      const data = await response.json();
      const vector = data?.data?.[0].embedding;
      
      if (vector) {
        // Service is working - mark as available if it was previously unavailable
        if (!this.isServiceAvailable) {
          this.isServiceAvailable = true;
          this.showServiceRestoredNotification();
        }
        
        this.cache.set(query, vector);
        return vector;
      }
    } catch (error) {
      console.error("Error fetching embedding:", error);
      
      // Mark service as unavailable if this is the first failure
      if (this.isServiceAvailable) {
        this.isServiceAvailable = false;
        this.lastFailureTime = Date.now();
        this.showServiceUnavailableNotification();
      }
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