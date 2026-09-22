import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { SearchProvider } from '@elastic/react-search-ui';
import { SearchDriver } from '@elastic/search-ui';
import EnhancedSearchBox from './EnhancedSearchBox';
import { embeddingService } from '../../search/EmbeddingService';

jest.mock('../../config/config-helper', () => ({
  getConfig: () => ({ embeddingModel: 'local-model' })
}));
jest.mock('../../search/EmbeddingService', () => ({
  embeddingService: { getEmbedding: jest.fn(), fetchEmbedding: jest.fn() }
}));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const type = (input, value) => fireEvent.change(input, { target: { value } });
const tick = async (ms = 1) => { await act(async () => { jest.advanceTimersByTime(ms); }); };
const finish = async request => { await act(async () => { request.resolve([1, 2]); }); await tick(); };
const searchedTerms = onSearch => onSearch.mock.calls.map(([state]) => state.searchTerm);

beforeEach(() => {
  jest.useFakeTimers();
  jest.resetAllMocks();
  embeddingService.getEmbedding.mockReturnValue(null);
  embeddingService.fetchEmbedding.mockResolvedValue([1, 2]);
});
afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
  jest.restoreAllMocks();
});

// Cover the deployed ReactDOM.render behavior and React 18 automatic batching.
describe.each([true, false])('with legacyRoot=%s', legacyRoot => {
  function mountSearch(props = {}) {
    const onSearch = jest.fn().mockResolvedValue({ results: [], totalResults: 0, totalPages: 0 });
    const config = {
      onSearch, trackUrlState: false, hasA11yNotifications: false,
      alwaysSearchOnInitialLoad: false
    };
    const driver = new SearchDriver(config);
    const mounted = render(
      <SearchProvider config={config} driver={driver}>
        <EnhancedSearchBox searchAsYouType {...props} />
      </SearchProvider>,
      { legacyRoot }
    );
    const input = screen.getByPlaceholderText('Search');
    act(() => { input.focus(); });
    return { ...mounted, driver, onSearch, input };
  }

  test('keeps newer typing when an older embedding completes before the next debounce', async () => {
    const old = deferred();
    embeddingService.fetchEmbedding.mockReturnValueOnce(old.promise);
    const { input, onSearch } = mountSearch();
    type(input, 'cleric');
    await tick(400);
    type(input, 'cleric soloing');
    await finish(old);
    expect(input).toHaveValue('cleric soloing');
    expect(searchedTerms(onSearch)).not.toContain('cleric');
    await tick(400);
    await tick();
    expect(searchedTerms(onSearch)).toEqual(['cleric soloing']);
  });

  test.each(['old first', 'new first'])('searches the newest draft when embeddings resolve %s', async order => {
    const old = deferred(), latest = deferred();
    embeddingService.fetchEmbedding.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise);
    const { input, onSearch } = mountSearch();
    type(input, 'cleric');
    await tick(400);
    type(input, 'cleric soloing');
    await tick(400);
    expect(embeddingService.fetchEmbedding).toHaveBeenLastCalledWith('cleric soloing', 'local-model');
    for (const request of order === 'old first' ? [old, latest] : [latest, old]) {
      await finish(request);
      expect(input).toHaveValue('cleric soloing');
    }
    expect(searchedTerms(onSearch)).toEqual(['cleric soloing']);
  });

  test('does not restore an obsolete query when its embedding fails', async () => {
    const old = deferred();
    const error = new Error('Old embedding failed');
    jest.spyOn(console, 'error').mockImplementation(() => {});
    embeddingService.fetchEmbedding.mockReturnValueOnce(old.promise);
    const { input, onSearch } = mountSearch();
    type(input, 'cleric');
    await tick(400);
    type(input, 'cleric soloing');
    await tick(400);
    await tick();
    await act(async () => { old.reject(error); });
    await tick();
    expect(input).toHaveValue('cleric soloing');
    expect(searchedTerms(onSearch)).toEqual(['cleric soloing']);
  });

  test('submits the latest draft with Enter while an earlier embedding is pending', async () => {
    const old = deferred();
    embeddingService.fetchEmbedding.mockReturnValueOnce(old.promise);
    const { input, onSearch } = mountSearch();
    type(input, 'cleric');
    await tick(400);
    type(input, 'cleric soloing');
    await act(async () => { fireEvent.submit(input.closest('form')); });
    await tick();
    await finish(old);
    await tick(400);
    expect(input).toHaveValue('cleric soloing');
    expect(searchedTerms(onSearch)).toEqual(['cleric soloing']);
  });

  test.each(['', '   '])('keeps a blank query %j when an earlier embedding completes', async blank => {
    const old = deferred();
    embeddingService.fetchEmbedding.mockReturnValueOnce(old.promise);
    const { input, onSearch } = mountSearch();
    type(input, 'cleric');
    await tick(400);
    type(input, blank);
    await tick(400);
    await finish(old);
    expect(input).toHaveValue(blank);
    expect(searchedTerms(onSearch)).toEqual([blank]);
  });

  test('cancels the intermediate draft when the user types back to the previous query', async () => {
    embeddingService.getEmbedding.mockReturnValue([1, 2]);
    const { input, onSearch } = mountSearch();
    type(input, 'cleric');
    await tick(400);
    await tick();
    onSearch.mockClear();
    type(input, 'cleric soloing');
    await tick(200);
    type(input, 'cleric');
    await tick(400);
    await tick();
    expect(input).toHaveValue('cleric');
    expect(searchedTerms(onSearch)).not.toContain('cleric soloing');
  });

  test('debounces rapid edits and does not overwrite them when previous search results arrive', async () => {
    embeddingService.getEmbedding.mockReturnValue([1, 2]);
    const results = deferred();
    const { input, onSearch } = mountSearch();
    onSearch.mockReturnValueOnce(results.promise);
    type(input, 'cleric');
    await tick(400);
    await tick();
    type(input, 'cleric s');
    await tick(200);
    type(input, 'cleric solo');
    await act(async () => { results.resolve({ results: [], totalResults: 0, totalPages: 0 }); });
    expect(input).toHaveValue('cleric solo');
    await tick(200);
    type(input, 'cleric soloing');
    await tick(400);
    await tick();
    expect(input).toHaveValue('cleric soloing');
    expect(searchedTerms(onSearch)).toEqual(['cleric', 'cleric soloing']);
  });

  test.each(['debounce', 'embedding'])('honors an external search term change during pending %s work', async phase => {
    const old = deferred();
    embeddingService.fetchEmbedding.mockReturnValueOnce(old.promise);
    const { input, onSearch, driver } = mountSearch();
    type(input, 'cleric');
    if (phase === 'embedding') await tick(400);
    act(() => { driver.getActions().setSearchTerm('restored from history'); });
    await tick();
    expect(input).toHaveValue('restored from history');
    await finish(old);
    await tick(400);
    expect(input).toHaveValue('restored from history');
    expect(searchedTerms(onSearch)).toEqual(['restored from history']);
  });

  test('distinguishes revisions even when the user returns to the same query text', async () => {
    const old = deferred(), latest = deferred();
    embeddingService.fetchEmbedding.mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise);
    const { input, onSearch } = mountSearch();
    type(input, 'cleric');
    await tick(400);
    type(input, 'cleric soloing');
    type(input, 'cleric');
    await tick(400);
    await finish(old);
    expect(onSearch).not.toHaveBeenCalled();
    await finish(latest);
    expect(input).toHaveValue('cleric');
    expect(searchedTerms(onSearch)).toEqual(['cleric']);
  });

  test.each(['debounce', 'embedding'])('cancels pending %s work on unmount', async phase => {
    const old = deferred();
    embeddingService.fetchEmbedding.mockReturnValue(old.promise);
    const { input, onSearch, unmount } = mountSearch();
    type(input, 'cleric');
    if (phase === 'embedding') await tick(400);
    unmount();
    await tick(400);
    await finish(old);
    expect(onSearch).not.toHaveBeenCalled();
    if (phase === 'debounce') expect(embeddingService.fetchEmbedding).not.toHaveBeenCalled();
  });

  test('supports manual search and falls back to text search when the latest embedding fails', async () => {
    const error = new Error('Embedding unavailable');
    const log = jest.spyOn(console, 'error').mockImplementation(() => {});
    embeddingService.fetchEmbedding.mockRejectedValue(error);
    const { input, onSearch } = mountSearch({ searchAsYouType: false });
    type(input, 'cleric');
    await tick(500);
    expect(embeddingService.fetchEmbedding).not.toHaveBeenCalled();
    expect(onSearch).not.toHaveBeenCalled();
    await act(async () => { fireEvent.submit(input.closest('form')); });
    await tick();
    expect(log).toHaveBeenCalledWith('Error fetching embedding:', error);
    expect(searchedTerms(onSearch)).toEqual(['cleric']);
  });
});
