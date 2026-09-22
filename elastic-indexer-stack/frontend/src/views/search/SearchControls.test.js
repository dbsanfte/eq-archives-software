import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import EnhancedSearchBox from './EnhancedSearchBox';
import SearchParameters from './SearchParameters';
import AdvancedSettings, { DEFAULT_KNN_PARAMS } from './AdvancedSettings';
import ArchiveStatusBar from '../ArchiveStatusBar';
import { embeddingService } from '../../search/EmbeddingService';

let mockContext;
let mockSearchBox;
jest.mock('@elastic/react-search-ui', () => ({
  withSearch: mapper => Component => props => <Component {...mapper(mockContext)} {...props} />,
  SearchBox: props => {
    mockSearchBox = props;
    return <input aria-label="Search" value={props.searchTerm}
      onChange={event => props.onChange(event.target.value)} onBlur={props.onBlur} />;
  }
}));
jest.mock('../../config/config-helper', () => ({
  getConfig: () => ({ indexName: 'eq-archive', embeddingModel: 'local-model' })
}));
jest.mock('../../search/EmbeddingService', () => ({
  embeddingService: {
    getEmbedding: jest.fn(), fetchEmbedding: jest.fn(), isEmbeddingServiceAvailable: jest.fn()
  }
}));

beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  mockContext = {
    searchTerm: '', setSearchTerm: jest.fn(), executeSearch: jest.fn(),
    searchAsYouType: true, autocompleteSuggestions: false
  };
  embeddingService.getEmbedding.mockReturnValue(null);
  embeddingService.fetchEmbedding.mockResolvedValue([1, 2]);
  embeddingService.isEmbeddingServiceAvailable.mockReturnValue(true);
});
afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
  jest.restoreAllMocks();
});

test('waits for embeddings before searching and ignores a concurrent submission', async () => {
  let complete;
  embeddingService.fetchEmbedding.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  render(<EnhancedSearchBox />);
  fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'cleric soloing' } });
  let pending;
  act(() => { pending = mockSearchBox.onSubmit('cleric soloing'); });
  expect(mockContext.setSearchTerm).not.toHaveBeenCalled();
  await act(async () => { await mockSearchBox.onSubmit('second query'); });
  expect(embeddingService.fetchEmbedding).toHaveBeenCalledTimes(1);
  await act(async () => { complete([1, 2]); await pending; });
  expect(mockContext.setSearchTerm).toHaveBeenCalledWith('cleric soloing');
});

test('debounces typing, uses cached embeddings, and skips empty terms', async () => {
  embeddingService.getEmbedding.mockReturnValue([1, 2]);
  render(<EnhancedSearchBox />);
  fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'necromancer' } });
  expect(mockContext.setSearchTerm).not.toHaveBeenCalled();
  await act(async () => { jest.advanceTimersByTime(400); });
  expect(mockContext.setSearchTerm).toHaveBeenCalledWith('necromancer');
  expect(embeddingService.fetchEmbedding).not.toHaveBeenCalled();
  await act(async () => { await mockSearchBox.onSubmit('  '); });
  expect(mockContext.setSearchTerm).toHaveBeenCalledWith('  ');
  await act(async () => { await mockSearchBox.onSubmit(undefined); });
  expect(mockContext.setSearchTerm).toHaveBeenCalledWith(undefined);
});

test('supports manual search and still submits when embeddings fail', async () => {
  mockContext.searchAsYouType = false;
  const error = new Error('Embedding service offline');
  embeddingService.fetchEmbedding.mockRejectedValue(error);
  const log = jest.spyOn(console, 'error').mockImplementation(() => {});
  render(<EnhancedSearchBox />);
  fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'everquest' } });
  await act(async () => { jest.advanceTimersByTime(500); });
  expect(mockContext.setSearchTerm).not.toHaveBeenCalled();
  await act(async () => { await mockSearchBox.onSubmit('everquest'); });
  expect(mockContext.setSearchTerm).toHaveBeenCalledWith('everquest');
  expect(log).toHaveBeenCalledWith('Error fetching embedding:', error);
});

test('protects in-progress typing from external changes and synchronizes again after blur', () => {
  mockContext.searchTerm = 'initial';
  const mounted = render(<EnhancedSearchBox />);
  fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'typed' } });
  mockContext.searchTerm = 'external';
  mounted.rerender(<EnhancedSearchBox />);
  expect(screen.getByLabelText('Search')).toHaveValue('typed');
  fireEvent.blur(screen.getByLabelText('Search'));
  mockContext.searchTerm = 'restored';
  mounted.rerender(<EnhancedSearchBox />);
  expect(screen.getByLabelText('Search')).toHaveValue('restored');
  fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'restored' } });
  mockContext.searchTerm = null;
  fireEvent.blur(screen.getByLabelText('Search'));
  mounted.rerender(<EnhancedSearchBox />);
  expect(screen.getByLabelText('Search')).toHaveValue('');
});

test('disables semantic controls while the embedding service is unavailable, then recovers', () => {
  const onChange = jest.fn();
  render(<SearchParameters values={{ enableSemanticSearch: true }} onChange={onChange} />);
  fireEvent.click(screen.getByRole('checkbox'));
  expect(onChange).toHaveBeenCalledWith('enableSemanticSearch', false);
  embeddingService.isEmbeddingServiceAvailable.mockReturnValue(false);
  act(() => jest.advanceTimersByTime(5000));
  expect(screen.getByRole('checkbox')).toBeDisabled();
  expect(screen.getByText('Semantic search is temporarily unavailable')).toBeInTheDocument();
  embeddingService.isEmbeddingServiceAvailable.mockReturnValue(true);
  act(() => jest.advanceTimersByTime(5000));
  expect(screen.getByRole('checkbox')).toBeEnabled();
});

test('commits vector parameters on blur and follows external changes', () => {
  const onChange = jest.fn();
  const mounted = render(<AdvancedSettings values={DEFAULT_KNN_PARAMS} onChange={onChange} />);
  for (const field of ['k', 'num_candidates', 'boost']) {
    const input = screen.getByLabelText(field);
    fireEvent.change(input, { target: { value: '25' } });
    expect(onChange).not.toHaveBeenCalledWith(field, '25');
    fireEvent.blur(input);
    expect(onChange).toHaveBeenCalledWith(field, '25');
  }
  mounted.rerender(<AdvancedSettings values={{ ...DEFAULT_KNN_PARAMS, k: 9, enableSemanticSearch: false }} onChange={onChange} />);
  expect(screen.getByLabelText('k')).toHaveValue('9');
  expect(screen.getByLabelText('k')).toBeDisabled();
});

test('shows archive counts and a rolling indexing rate without browser credentials', async () => {
  const originalFetch = global.fetch;
  const counts = [100, 120, 130, 140, 150, 160, 170, 180, 190];
  global.fetch = jest.fn().mockImplementation(async () => ({ json: async () => ({ count: counts.shift() }) }));
  try {
    await act(async () => { render(<ArchiveStatusBar />); });
    expect(screen.getByText('Documents Indexed: 100')).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith('/elasticsearch/eq-archive/_count');
    await act(async () => { jest.advanceTimersByTime(5000); });
    expect(screen.queryByText(/Indexing Rate:/)).not.toBeInTheDocument();
    await act(async () => { jest.advanceTimersByTime(5000); });
    expect(screen.getByText('Indexing Rate: 180.00 docs/min')).toBeInTheDocument();
    for (let i = 0; i < 6; i++) await act(async () => { jest.advanceTimersByTime(5000); });
    expect(screen.getByText('Documents Indexed: 190')).toBeInTheDocument();
    expect(screen.getByText('Indexing Rate: 120.00 docs/min')).toBeInTheDocument();
  } finally { global.fetch = originalFetch; }
});

test('handles missing count data and reports count request failures', async () => {
  const originalFetch = global.fetch;
  const error = new Error('Search unavailable');
  const log = jest.spyOn(console, 'error').mockImplementation(() => {});
  global.fetch = jest.fn().mockResolvedValueOnce({ json: async () => ({}) }).mockRejectedValue(error);
  try {
    await act(async () => { render(<ArchiveStatusBar />); });
    expect(screen.getByText('Documents Indexed: 0')).toBeInTheDocument();
    await act(async () => { jest.advanceTimersByTime(5000); });
    expect(log).toHaveBeenCalledWith('Error fetching archive stats:', error);
  } finally { global.fetch = originalFetch; }
});
