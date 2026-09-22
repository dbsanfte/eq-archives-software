import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import CaptureHistory, { captureDate } from './CaptureHistory';
import { fetchCaptures } from '../../search/CaptureService';
import { captureIdentity } from '../../search/CaptureIdentity';

jest.mock('../../search/CaptureService');
const result = { _meta: { id: 'websites/example.org/20000101000000/a' }, url: { raw: 'https://web.archive.org/web/20000101000000/http://example.org/a' }, title: { raw: 'First version' }, capture_date: { raw: '2000-01-01T10:00:00' } };
const identity = captureIdentity(result);
const page = { records: [result], nextOffset: null, limited: false };
const toggle = () => fireEvent.click(screen.getByRole('button', { name: /captures/i }));
beforeEach(() => {
  jest.clearAllMocks();
  fetchCaptures.mockResolvedValue(page);
  global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ hits: { hits: [{ _id: result._meta.id, _source: { text_full: '# Original document' } }] } }) });
});

test('loads dated captures on demand, previews the exact selected record, and reuses loaded history', async () => {
  render(<CaptureHistory identity={identity} />);
  expect(fetchCaptures).not.toHaveBeenCalled();
  toggle();
  expect(screen.getByRole('status')).toHaveTextContent('Loading captures');
  expect(await screen.findByText('First version')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Hide captures (1)' })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByText(/outside your current search filters/)).toBeVisible();
  expect(screen.getByRole('link', { name: 'Open capture from 2000-01-01' })).toHaveAttribute('href', result.url.raw);
  fireEvent.click(screen.getByRole('button', { name: 'Preview capture from 2000-01-01' }));
  expect(await screen.findByRole('heading', { name: 'Original document' })).toBeVisible();
  expect(JSON.parse(fetch.mock.calls[0][1].body).query.ids.values).toEqual([result._meta.id]);
  expect(screen.getByRole('heading', { name: 'Capture · 2000-01-01' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  toggle();
  expect(screen.queryByText('First version')).not.toBeInTheDocument();
  toggle();
  expect(screen.getByText('First version')).toBeVisible();
  expect(fetchCaptures).toHaveBeenCalledTimes(1);
});

test('retries errors safely and appends later pages without duplicating captures', async () => {
  fetchCaptures.mockRejectedValueOnce(new Error('private upstream error')).mockResolvedValueOnce({ ...page, nextOffset: 50 });
  render(<CaptureHistory identity={identity} />);
  toggle();
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load captures');
  expect(screen.queryByText(/private upstream/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  expect(await screen.findByText('First version')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Hide captures (1+)' })).toBeVisible();
  fetchCaptures.mockResolvedValueOnce({ records: [result, { ...result, _meta: { id: 'another' }, title: {}, url: {}, capture_date: {} }], nextOffset: null, limited: true });
  fireEvent.click(screen.getByRole('button', { name: 'Load more captures' }));
  expect(await screen.findByText('Untitled capture')).toBeVisible();
  expect(screen.getAllByText('First version')).toHaveLength(1);
  expect(screen.getByText('Date unknown')).toBeVisible();
  expect(screen.getByText(/first 1,000 records/)).toBeVisible();
  expect(fetchCaptures.mock.calls[2][1]).toBe(50);
});

test('cancels collapsed histories and ignores late successful or failed responses', async () => {
  let resolve, reject;
  fetchCaptures.mockImplementationOnce(() => new Promise(done => { resolve = done; }))
    .mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }));
  const mounted = render(<CaptureHistory identity={identity} />);
  toggle();
  const firstSignal = fetchCaptures.mock.calls[0][2];
  toggle();
  expect(firstSignal.aborted).toBe(true);
  await act(async () => resolve(page));
  toggle();
  const secondSignal = fetchCaptures.mock.calls[1][2];
  mounted.unmount();
  expect(secondSignal.aborted).toBe(true);
  await act(async () => reject(new Error('late failure')));
});

test('handles an unavailable history and non-string or missing dates', async () => {
  fetchCaptures.mockResolvedValueOnce({ ...page, records: [] });
  render(<CaptureHistory identity={identity} />);
  toggle();
  expect(await screen.findByText('No other capture records are available.')).toBeVisible();
  expect(captureDate({})).toBe('Date unknown');
  expect(captureDate({ capture_date: { raw: 123 } })).toBe('Date unknown');
  expect(captureDate({ capture_date: { raw: 'unparsed' } })).toBe('Date unknown');
});
