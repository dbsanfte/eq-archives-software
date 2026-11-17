import React from 'react';
import { render, screen } from '@testing-library/react';
import SearchProgress from './SearchProgress';

test.each([
  ['waiting', 'Waiting for typing to finish'], ['preparing', 'Preparing semantic search'],
  ['searching', 'Searching the archive'], ['reranking', 'Reranking matches'], ['idle', 'Searching the archive']
])('shows a visible accessible indication during %s', (phase, label) => {
  const view = render(<SearchProgress phase={phase} isLoading hasResults />);
  expect(screen.getByRole('status')).toHaveTextContent(label);
  expect(screen.getByRole('status')).toHaveTextContent('Showing previous results');
  expect(screen.getByRole('progressbar', { name: 'Search progress' })).toBeInTheDocument();
  view.rerender(<SearchProgress phase={phase} isLoading={false} hasResults />);
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

test('the first search does not claim to be showing previous results', () => {
  render(<SearchProgress phase="preparing" isLoading hasResults={false} />);
  expect(screen.getByRole('status')).not.toHaveTextContent('Showing previous results');
});
