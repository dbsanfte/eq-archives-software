import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import SearchError from './SearchError';
import { BUSY_MESSAGE } from '../../search/ArchiveFetch';

test('preserves results when no error exists', () => {
  render(<SearchError><p>Existing results</p></SearchError>);
  expect(screen.getByText('Existing results')).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

test('shows a safe explanation with a working retry control', () => {
  const retry = jest.fn();
  render(<SearchError error={BUSY_MESSAGE} onRetry={retry} />);
  expect(screen.getByRole('alert')).toHaveTextContent(BUSY_MESSAGE);
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  expect(retry).toHaveBeenCalledTimes(1);
});

test('never displays unknown backend details', () => {
  render(<SearchError error={new Error('secret upstream traceback')} />);
  expect(screen.getByRole('alert')).toHaveTextContent('temporarily unavailable');
  expect(screen.queryByText(/secret/)).not.toBeInTheDocument();
});

test('recognizes wrapped Search UI errors while discarding surrounding diagnostics', () => {
  render(<SearchError error={`An unexpected error occurred: ${BUSY_MESSAGE} private traceback`} />);
  expect(screen.getByRole('alert')).toHaveTextContent(BUSY_MESSAGE);
  expect(screen.getByRole('alert')).not.toHaveTextContent('private traceback');
});
