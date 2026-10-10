import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import RecentSites from './RecentSites';
import { fetchRecentSites, siteSearchUrl } from '../../search/RecentSitesService';
import SiteSearchScope from '../search/SiteSearchScope';

jest.mock('../../search/RecentSitesService', () => ({
  ...jest.requireActual('../../search/RecentSitesService'), fetchRecentSites: jest.fn()
}));
jest.mock('../ArchiveStatusBar', () => () => <span>Archive status</span>);
const site = { domain: 'eq.example.org', indexedAt: Date.UTC(2026, 9, 10, 9, 30), records: 123,
  firstCapture: Date.UTC(1999, 0, 1), lastCapture: Date.UTC(2006, 0, 1), partial: false };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

beforeEach(() => {
  jest.clearAllMocks();
  fetchRecentSites.mockResolvedValue([site]);
});
afterEach(() => jest.useRealTimers());

test('shows loading, domain cards, indexing/capture dates, and exact site search links in server order', async () => {
  const pending = deferred();
  fetchRecentSites.mockReturnValue(pending.promise);
  render(<RecentSites />);
  expect(screen.getByRole('button', { name: 'Loading…' })).toBeDisabled();
  expect(screen.getByRole('status')).toHaveTextContent('Checking');
  expect(screen.queryByText(/No indexed websites/)).not.toBeInTheDocument();
  await act(async () => pending.resolve([site, { ...site, domain: 'older.org', records: 1, firstCapture: site.lastCapture }]));
  const list = screen.getByRole('list', { name: 'Recently indexed sites' });
  const cards = within(list).getAllByRole('link');
  expect(cards.map(card => card.getAttribute('href'))).toEqual([siteSearchUrl(site.domain), siteSearchUrl('older.org')]);
  expect(cards[0]).toHaveTextContent('123 indexed records');
  expect(cards[0]).toHaveTextContent('Captures from 1999–2006');
  expect(cards[0]).toHaveTextContent('10 Oct 2026, 09:30 UTC');
  expect(cards[0].querySelector('time')).toHaveAttribute('datetime', '2026-10-10T09:30:00.000Z');
  expect(cards[1]).toHaveTextContent('1 indexed record');
  expect(cards[1]).toHaveTextContent('Captures from 2006');
  expect(screen.getByRole('status')).toHaveTextContent('List updated');
  expect(fetchRecentSites).toHaveBeenCalledTimes(1);
});

test('missing dates and partial counts remain honestly labelled', async () => {
  fetchRecentSites.mockResolvedValue([{ ...site, partial: true }, { ...site, domain: 'undated.org', firstCapture: null, lastCapture: null }]);
  render(<RecentSites />);
  expect(await screen.findByText('At least 123 indexed records')).toBeVisible();
  expect(screen.queryByText(/Captures from/)).not.toBeInTheDocument();
});

test('empty state differs from failure, and refresh can populate an initially empty list', async () => {
  fetchRecentSites.mockResolvedValueOnce([]);
  render(<RecentSites />);
  expect(await screen.findByText(/No indexed websites/)).toBeVisible();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh list' }));
  expect(await screen.findByRole('link', { name: 'Search captures from eq.example.org' })).toBeVisible();
  expect(screen.queryByText(/No indexed websites/)).not.toBeInTheDocument();
});

test('retry recovers from failure and preserves the last good list when a refresh fails', async () => {
  fetchRecentSites.mockRejectedValueOnce(new Error('private upstream details'));
  render(<RecentSites />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not update');
  expect(screen.queryByText(/private upstream/)).not.toBeInTheDocument();
  expect(screen.queryByText(/No indexed websites/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh list' }));
  const link = await screen.findByRole('link', { name: 'Search captures from eq.example.org' });
  fetchRecentSites.mockRejectedValueOnce(new Error('failed'));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh list' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('previous list is still shown');
  expect(link).toBeVisible();
});

test.each(['resolve', 'reject'])('leaving aborts the request and ignores late %s without disturbing a fresh view', async finish => {
  const pending = deferred();
  fetchRecentSites.mockReturnValueOnce(pending.promise);
  const first = render(<RecentSites />);
  const signal = fetchRecentSites.mock.calls[0][0];
  first.unmount();
  expect(signal.aborted).toBe(true);
  render(<RecentSites />);
  await screen.findByRole('link', { name: 'Search captures from eq.example.org' });
  await act(async () => pending[finish]([]));
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByText(/No indexed websites/)).not.toBeInTheDocument();
});

test('a slow request times out, offers retry, and cannot leave the view loading forever', async () => {
  jest.useFakeTimers();
  fetchRecentSites.mockImplementationOnce(signal => new Promise((_, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
  }));
  render(<RecentSites />);
  await act(async () => jest.advanceTimersByTime(15000));
  expect(screen.getByRole('alert')).toHaveTextContent('Could not update');
  expect(screen.getByRole('button', { name: 'Refresh list' })).toBeEnabled();
});

test('site scope follows actual search filters and clears only the domain filter', async () => {
  const removeFilter = jest.fn();
  const { rerender } = render(<SiteSearchScope removeFilter={removeFilter} />);
  expect(screen.queryByRole('region')).not.toBeInTheDocument();
  rerender(<SiteSearchScope removeFilter={removeFilter} filters={[
    { field: 'domain_name', values: [site.domain, 'second.org'], type: 'any' },
    { field: 'capture_date', values: [{ from: '1999', to: '2006' }], type: 'all' },
    { field: 'domain_name', values: ['excluded.org'], type: 'none' },
    { field: 'domain_name', values: [null, 123], type: 'any' }
  ]} />);
  expect(screen.getByRole('region', { name: 'Site search scope' })).toHaveTextContent('eq.example.org, second.org');
  expect(screen.queryByText('excluded.org')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Search all sites' }));
  expect(removeFilter).toHaveBeenCalledWith('domain_name');
  rerender(<SiteSearchScope filters={[]} />);
  await waitFor(() => expect(screen.queryByRole('region')).not.toBeInTheDocument());
});
