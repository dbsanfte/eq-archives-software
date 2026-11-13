// Only exact IDs leave the result list. Text and credentials stay in the API.
export function relevanceSearch(state) {
  const sort = state.sortList?.[0];
  return Boolean(state.searchTerm?.trim()) &&
    (state.sortField || sort?.field || '_score') === '_score' &&
    (state.sortDirection || sort?.direction || 'desc') === 'desc';
}

export async function rerankResults(query, results, index) {
  const ids = results.map(result => result._meta?.id);
  if (ids.length < 2 || ids.some(id => typeof id !== 'string' || !id) || new Set(ids).size !== ids.length) return results;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4500);
  try {
    const response = await fetch(`/elasticsearch/${index}/_rerank`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, ids }), signal: controller.signal
    });
    if (!response.ok) return results;
    const data = await response.json();
    if (!Array.isArray(data.ids) || data.ids.length !== ids.length || new Set(data.ids).size !== ids.length ||
        data.ids.some(id => !ids.includes(id))) return results;
    const byId = new Map(results.map(result => [result._meta.id, result]));
    return data.ids.map(id => byId.get(id));
  } catch (_) {
    return results;
  } finally {
    clearTimeout(timer);
  }
}
