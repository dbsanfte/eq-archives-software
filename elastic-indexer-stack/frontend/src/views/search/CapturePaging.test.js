import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { CapturePaging, CaptureSummary } from './CapturePaging';

test('uses previous/next controls without inventing a total number of groups', () => {
  const onChange = jest.fn();
  const view = render(<CapturePaging current={2} totalPages={3} onChange={onChange} />);
  fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  expect(onChange.mock.calls).toEqual([[1], [3]]);
  view.rerender(<CapturePaging current={1} totalPages={1} onChange={onChange} />);
  expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  view.rerender(<CapturePaging current={2} totalPages={3} onChange={onChange} isLoading />);
  expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
});

test('distinguishes source cards from capture counts and explains bounded results', () => {
  const view = render(<CaptureSummary totalResults={40} pagingStart={1} pagingEnd={20} />);
  expect(screen.getByText('Showing sources 1–20 · 40 matching captures')).toBeVisible();
  expect(screen.queryByText(/1,000/)).not.toBeInTheDocument();
  view.rerender(<CaptureSummary totalResults={10000} pagingStart={1} pagingEnd={20} rawResponse={{ captureGroups: { windowReached: true } }} />);
  expect(screen.getByText(/10,000\+ matching captures/)).toBeVisible();
  expect(screen.getByText(/Reached the browsing limit/)).toBeVisible();
  view.rerender(<CaptureSummary totalResults={2000} pagingStart={1} pagingEnd={20} />);
  expect(screen.getByText(/Groups cover the first 1,000/)).toBeVisible();
  view.rerender(<CaptureSummary totalResults={0} pagingStart={0} pagingEnd={0} />);
  expect(screen.getByText('No sources found · 0 matching captures')).toBeVisible();
});
