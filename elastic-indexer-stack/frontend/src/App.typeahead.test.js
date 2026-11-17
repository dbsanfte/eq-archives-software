import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import App from './App';
import { getSearchConfig } from './search/Connector';
import { embeddingService } from './search/EmbeddingService';

jest.mock('./search/Connector', () => ({ getSearchConfig: jest.fn() }));
jest.mock('./config/config-helper', () => ({
  getConfig: () => ({ embeddingModel: 'local-model', vectorFields: ['vector'], titleField: 'title', urlField: 'url' }),
  getStandardFacetFields: () => [],
  getDatePickerFacetFields: () => [],
  buildSortOptionsFromConfig: () => []
}));
jest.mock('./search/EmbeddingService', () => ({
  embeddingService: { getEmbedding: jest.fn(), fetchEmbedding: jest.fn(), isEmbeddingServiceAvailable: jest.fn() }
}));
jest.mock('./views/ArchiveStatusBar', () => () => null);
jest.mock('./views/HeaderContent', () => () => null);

const tick = async ms => { await act(async () => { jest.advanceTimersByTime(ms); }); };

beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  embeddingService.isEmbeddingServiceAvailable.mockReturnValue(true);
  embeddingService.getEmbedding.mockReturnValue(null);
  embeddingService.fetchEmbedding.mockResolvedValue([1, 2]);
});
afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
});

describe.each([true, false])('full search controls with legacyRoot=%s', legacyRoot => {
  test.each([
    ['debounce', 'Advanced...'], ['embedding', 'Advanced...'],
    ['debounce', 'Search Syntax...'], ['embedding', 'Search Syntax...']
  ])('continues automatic search during %s when opening %s', async (phase, panel) => {
    const onSearch = jest.fn().mockResolvedValue({ results: [], totalResults: 0, totalPages: 0 });
    getSearchConfig.mockReturnValue({ onSearch, trackUrlState: false, hasA11yNotifications: false,
      alwaysSearchOnInitialLoad: false });
    let finishEmbedding;
    embeddingService.fetchEmbedding.mockImplementation(() => new Promise(resolve => { finishEmbedding = resolve; }));
    render(<App />, { legacyRoot });
    const input = screen.getByPlaceholderText('Search');
    fireEvent.change(input, { target: { value: 'cleric healing' } });
    await tick(phase === 'debounce' ? 200 : 400);
    const signal = embeddingService.fetchEmbedding.mock.calls[0]?.[2].signal;
    fireEvent.click(screen.getByRole('button', { name: panel }));
    expect(screen.getByPlaceholderText('Search')).toBe(input);
    if (signal) expect(signal.aborted).toBe(false);
    await tick(400);
    await act(async () => { finishEmbedding([1, 2]); });
    await tick(1);
    expect(input).toHaveValue('cleric healing');
    expect(onSearch.mock.calls.map(([state]) => state.searchTerm)).toEqual(['cleric healing']);
  });

  test('applies the newest semantic setting without cancelling an automatic search', async () => {
    const onSearch = jest.fn().mockResolvedValue({ results: [], totalResults: 0, totalPages: 0 });
    getSearchConfig.mockReturnValue({ onSearch, trackUrlState: false, hasA11yNotifications: false,
      alwaysSearchOnInitialLoad: false });
    let finishEmbedding;
    embeddingService.fetchEmbedding.mockImplementation(() => new Promise(resolve => { finishEmbedding = resolve; }));
    render(<App />, { legacyRoot });
    fireEvent.click(screen.getByRole('button', { name: 'Advanced...' }));
    const input = screen.getByPlaceholderText('Search');
    fireEvent.change(input, { target: { value: 'wizard epic quest' } });
    await tick(400);
    const signal = embeddingService.fetchEmbedding.mock.calls[0][2].signal;
    fireEvent.click(screen.getByRole('checkbox', { name: 'Enable semantic search' }));
    expect(screen.getByPlaceholderText('Search')).toBe(input);
    expect(signal.aborted).toBe(false);
    await act(async () => { finishEmbedding([1, 2]); });
    await tick(1);
    expect(onSearch.mock.calls.map(([state]) => state.searchTerm)).toEqual(['wizard epic quest']);
    expect(getSearchConfig.mock.calls[0][0].current.enableSemanticSearch).toBe(false);
  });
});
