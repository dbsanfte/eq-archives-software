import { embeddingService } from './EmbeddingService';

// Mock fetch globally
global.fetch = jest.fn();

// Mock document for DOM operations
document.body.innerHTML = '';

describe('EmbeddingService', () => {
  beforeEach(() => {
    // Reset service state before each test
    embeddingService.cache.clear();
    embeddingService.pendingQueries.clear();
    embeddingService.isServiceAvailable = true;
    embeddingService.lastFailureTime = null;
    
    // Clear fetch mocks
    fetch.mockClear();
    
    // Clear DOM
    document.body.innerHTML = '';
  });

  describe('constructor', () => {
    it('initializes with proper default values', () => {
      expect(embeddingService.cache).toBeInstanceOf(Map);
      expect(embeddingService.pendingQueries).toBeInstanceOf(Map);
      expect(embeddingService.isServiceAvailable).toBe(true);
      expect(embeddingService.lastFailureTime).toBe(null);
    });
  });

  describe('fetchEmbedding', () => {
    it('returns null for empty query', async () => {
      const result = await embeddingService.fetchEmbedding('');
      expect(result).toBeNull();
    });

    it('returns cached embedding when available', async () => {
      const testVector = [0.1, 0.2, 0.3];
      embeddingService.cache.set('test query', testVector);
      
      const result = await embeddingService.fetchEmbedding('test query');
      expect(result).toEqual(testVector);
      expect(fetch).not.toHaveBeenCalled();
    });

    it('makes API request for uncached query', async () => {
      const mockResponse = {
        data: [{ embedding: [0.4, 0.5, 0.6] }]
      };
      fetch.mockResolvedValueOnce({
        json: () => Promise.resolve(mockResponse)
      });
      
      const result = await embeddingService.fetchEmbedding('new query');
      expect(result).toEqual([0.4, 0.5, 0.6]);
      expect(fetch).toHaveBeenCalledWith(
        "/openai/v1/embeddings",
        expect.objectContaining({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ input: ['new query'], model: undefined })
        })
      );
    });

    it('adds the Nomic query prefix while caching the original search text', async () => {
      const model = 'text-embedding-nomic-embed-text-v1.5@q8_0';
      const vector = [0.4, 0.5, 0.6];
      fetch.mockResolvedValueOnce({ json: async () => ({ data: [{ embedding: vector }] }) });

      await embeddingService.fetchEmbedding('ancient cyclops', model);

      expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
        input: ['search_query: ancient cyclops'], model
      });
      expect(embeddingService.getEmbedding('ancient cyclops')).toEqual(vector);
    });

    it('preserves the input for other embedding models', async () => {
      fetch.mockResolvedValueOnce({ json: async () => ({ data: [{ embedding: [1, 0, 0] }] }) });
      await embeddingService.fetchEmbedding('ancient cyclops', 'another-embedding-model');
      expect(JSON.parse(fetch.mock.calls[0][1].body).input).toEqual(['ancient cyclops']);
    });

    it('handles API timeout', async () => {
      fetch.mockImplementationOnce(() => new Promise(resolve => 
        setTimeout(() => resolve({ json: () => ({}) }), 6000)
      ));

      const result = await embeddingService.fetchEmbedding('timeout query');
      expect(result).toBeNull();
      expect(embeddingService.isServiceAvailable).toBe(false);
    }, 10000);

    it('handles API errors', async () => {
      fetch.mockRejectedValueOnce(new Error('API error'));
      
      const result = await embeddingService.fetchEmbedding('error query');
      expect(result).toBeNull();
      expect(embeddingService.isServiceAvailable).toBe(false);
    });

    it('skips requests during cooldown period', async () => {
      embeddingService.isServiceAvailable = false;
      embeddingService.lastFailureTime = Date.now() - 30000; // 30s ago
      
      const result = await embeddingService.fetchEmbedding('test');
      expect(result).toBeNull();
      expect(fetch).not.toHaveBeenCalled();
    });

    it('retries after cooldown period', async () => {
      embeddingService.isServiceAvailable = false;
      embeddingService.lastFailureTime = Date.now() - 70000; // 70s ago
      
      const mockResponse = { data: [{ embedding: [0.7, 0.8, 0.9] }] };
      fetch.mockResolvedValueOnce({ json: () => Promise.resolve(mockResponse) });
      
      const result = await embeddingService.fetchEmbedding('retry query');
      expect(result).toEqual([0.7, 0.8, 0.9]);
      expect(embeddingService.isServiceAvailable).toBe(true);
    });
  });

  describe('notification system', () => {
    beforeEach(() => {
      jest.useFakeTimers();
    });

    afterEach(() => {
      jest.useRealTimers();
    });

    it('creates notification container', () => {
      embeddingService.createNotificationContainer();
      const container = document.getElementById('embedding-service-notifications');
      expect(container).not.toBeNull();
      expect(container.style.position).toBe('fixed');
    });

    it('shows service unavailable notification', () => {
      embeddingService.createNotificationContainer();
      embeddingService.showServiceUnavailableNotification();
      const notifications = document.querySelectorAll('#embedding-service-notifications > div');
      expect(notifications.length).toBe(1);
      expect(notifications[0].textContent).toContain('unavailable');
      expect(notifications[0].style.backgroundColor).toBe('rgb(255, 243, 205)');
    });

    it('shows service restored notification', () => {
      embeddingService.createNotificationContainer();
      embeddingService.showServiceRestoredNotification();
      const notifications = document.querySelectorAll('#embedding-service-notifications > div');
      expect(notifications.length).toBe(1);
      expect(notifications[0].textContent).toContain('restored');
      expect(notifications[0].style.backgroundColor).toBe('rgb(212, 237, 218)');
    });

    it('auto-dismisses notifications', () => {
      embeddingService.createNotificationContainer();
      embeddingService.showNotification('Test notification', 'warning', 5000);
      const notifications = document.querySelectorAll('#embedding-service-notifications > div');
      expect(notifications.length).toBe(1);
      
      jest.advanceTimersByTime(6000);
      
      // Notification should be removed after 6s (5s duration + 0.5s fade)
      expect(document.querySelectorAll('#embedding-service-notifications > div').length).toBe(0);
    });
  });

  describe('utility methods', () => {
    it('getEmbedding returns cached value', () => {
      const testVector = [1.0, 2.0, 3.0];
      embeddingService.cache.set('test', testVector);
      expect(embeddingService.getEmbedding('test')).toEqual(testVector);
    });

    it('clearOldEmbeddings maintains cache size', () => {
      // Fill cache with 60 items
      for (let i = 0; i < 60; i++) {
        embeddingService.cache.set(`query-${i}`, new Array(128).fill(0));
      }
      
      embeddingService.clearOldEmbeddings(50);
      expect(embeddingService.cache.size).toBe(50);
      expect(embeddingService.cache.has('query-0')).toBe(false);
      expect(embeddingService.cache.has('query-59')).toBe(true);
    });

    it('shouldRetryService respects cooldown', () => {
      embeddingService.lastFailureTime = Date.now();
      expect(embeddingService.shouldRetryService()).toBe(false);
      
      // Advance time past cooldown
      const realNow = Date.now;
      Date.now = jest.fn(() => realNow() + 61000);
      expect(embeddingService.shouldRetryService()).toBe(true);
      Date.now = realNow;
    });

    it('isEmbeddingServiceAvailable returns correct status', () => {
      embeddingService.isServiceAvailable = false;
      embeddingService.lastFailureTime = Date.now();
      expect(embeddingService.isEmbeddingServiceAvailable()).toBe(false);
      
      // After cooldown period
      const realNow = Date.now;
      Date.now = jest.fn(() => realNow() + 61000);
      expect(embeddingService.isEmbeddingServiceAvailable()).toBe(true);
      Date.now = realNow;
    });
  });
});