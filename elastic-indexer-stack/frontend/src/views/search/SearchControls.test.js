import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import SearchParameters from './SearchParameters';
import AdvancedSettings, { DEFAULT_KNN_PARAMS } from './AdvancedSettings';
import ArchiveStatusBar from '../ArchiveStatusBar';
import { embeddingService } from '../../search/EmbeddingService';

jest.mock('../../config/config-helper', () => ({
  getConfig: () => ({ indexName: 'eq-archive', embeddingModel: 'local-model' })
}));
jest.mock('../../search/EmbeddingService', () => ({
  embeddingService: { isEmbeddingServiceAvailable: jest.fn() }
}));

beforeEach(() => {
  jest.useFakeTimers();
  jest.clearAllMocks();
  embeddingService.isEmbeddingServiceAvailable.mockReturnValue(true);
});
afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
  jest.restoreAllMocks();
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
