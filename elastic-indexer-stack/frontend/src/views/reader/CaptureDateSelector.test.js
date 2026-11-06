import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import CaptureDateSelector from './CaptureDateSelector';
import { fetchCaptures } from '../../search/CaptureService';
jest.mock('../../search/CaptureService', () => ({ fetchCaptures: jest.fn() }));
const record = { _meta: { id: 'selected' }, capture_date: { raw: '2006-12-13T16:00:00Z' } };
const earlier = { _meta: { id: 'earlier' }, capture_date: { raw: '2005-01-01T12:00:00' } };
const identity = { key: 'page:same' };
beforeEach(() => fetchCaptures.mockReset());

test('switches exact capture IDs and retains search navigation and literal find context', async () => {
  fetchCaptures.mockResolvedValue({ records: [record, earlier], nextOffset: null });
  const navigate = jest.fn();
  render(<CaptureDateSelector record={record} identity={identity} find="a passage" returnTo="/?q=zone&current=2" part="text" navigate={navigate} />);
  await screen.findByRole('option', { name: '2005-01-01 12:00:00 UTC' });
  fireEvent.change(screen.getByRole('combobox', { name: 'Capture date' }), { target: { value: 'earlier' } });
  const target = new URL(navigate.mock.calls[0][0], window.location.origin);
  expect(target.searchParams.get('id')).toBe('earlier');
  expect(target.searchParams.get('find')).toBe('a passage');
  expect(target.searchParams.get('return')).toBe('/?q=zone&current=2');
});

test('retains selected captures outside the first batch and loads more bounded dates', async () => {
  fetchCaptures.mockResolvedValueOnce({ records: [earlier], nextOffset: 50 }).mockResolvedValueOnce({ records: [{ _meta: { id: 'unknown' } }], nextOffset: null, limited: true });
  render(<CaptureDateSelector record={record} identity={identity} />);
  await screen.findByRole('button', { name: 'Load more capture dates' });
  expect(screen.getByRole('combobox').value).toBe('selected');
  fireEvent.click(screen.getByRole('button', { name: 'Load more capture dates' }));
  await screen.findByRole('option', { name: 'Date unknown' });
  expect(fetchCaptures).toHaveBeenLastCalledWith(identity, 50, expect.any(AbortSignal));
  expect(screen.getByText(/first 1,000/)).toBeVisible();
});

test('a failed date lookup preserves the current document and can be retried', async () => {
  fetchCaptures.mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ records: [], nextOffset: null });
  render(<CaptureDateSelector record={record} identity={identity} />);
  await screen.findByRole('alert');fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  await screen.findByText('Only one known capture of this page.');
  expect(screen.getByRole('combobox').value).toBe('selected');
});

test('late capture responses cannot populate a newly selected page', async () => {
  let resolve;
  fetchCaptures.mockImplementationOnce(() => new Promise(done => { resolve = done; })).mockResolvedValue({ records: [earlier], nextOffset: null });
  const view = render(<CaptureDateSelector key="first" record={record} identity={identity} />);
  view.rerender(<CaptureDateSelector key="second" record={earlier} identity={{ key: 'page:different' }} />);
  await screen.findByText('Only one known capture of this page.');
  await act(async () => resolve({ records: [record], nextOffset: null }));
  expect(screen.queryByRole('option', { name: /2006-12-13/ })).not.toBeInTheDocument();
});
