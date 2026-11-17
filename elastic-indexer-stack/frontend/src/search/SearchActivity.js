import { createContext, useContext, useSyncExternalStore } from 'react';

const idle = { phase: 'idle', display: null };
export const SearchActivityContext = createContext(null);
const subscribeIdle = () => () => {};
const getIdle = () => idle;

export function useSearchActivity() {
  const activity = useContext(SearchActivityContext);
  return useSyncExternalStore(activity?.subscribe || subscribeIdle,
    activity?.getSnapshot || getIdle, getIdle);
}

// Search UI can deliver an obsolete empty response while the next request is
// still running. Only the current run may replace the visible, completed page.
export function createSearchActivity() {
  let snapshot = idle, revision = 0;
  const listeners = new Set();
  const publish = patch => {
    snapshot = { ...snapshot, ...patch };
    listeners.forEach(listener => listener());
  };
  return {
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
    getSnapshot: () => snapshot,
    draft(query) {
      revision += 1;
      publish({ query, phase: 'waiting' });
    },
    preparing(query) {
      if (snapshot.query === query) publish({ phase: 'preparing' });
    },
    async search(state, perform) {
      const request = ++revision;
      publish({ query: state.searchTerm, phase: 'searching' });
      const progress = phase => {
        if (request === revision && snapshot.phase !== phase) publish({ phase });
      };
      try {
        const response = await perform({ ...state, _archiveProgress: progress });
        if (request === revision) {
          const current = response.current ?? state.current ?? 1;
          const size = state.resultsPerPage || 20;
          const start = (current - 1) * size;
          publish({ phase: 'idle', display: {
            ...response, current, resultSearchTerm: state.searchTerm, wasSearched: true,
            pagingStart: response.pagingStart ?? (response.results.length ? start + 1 : 0),
            pagingEnd: response.pagingEnd ?? (response.results.length ? start + response.results.length : 0),
            totalPages: response.totalPages ?? Math.max(1, Math.ceil(response.totalResults / size))
          } });
        }
        return response;
      } catch (error) {
        if (request !== revision) return { results: [], totalResults: 0 };
        publish({ phase: 'error' });
        throw error;
      }
    }
  };
}
