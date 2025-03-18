class EmbeddingService {
  constructor() {
    this.cache = new Map();
    this.pendingQueries = new Map();
  }

  async fetchEmbedding(query, model) {
    if (!query || this.cache.has(query)) return this.cache.get(query);
    
    try {
      // Show that we're fetching this query
      console.log(`Fetching embedding for: ${query}`);
      
      const response = await fetch("/openai/v1/embeddings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: [query], model })
      });
      
      const data = await response.json();
      const vector = data?.data?.[0].embedding;
      
      if (vector) {
        this.cache.set(query, vector);
        return vector;
      }
    } catch (error) {
      console.error("Error fetching embedding:", error);
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
}

export const embeddingService = new EmbeddingService();