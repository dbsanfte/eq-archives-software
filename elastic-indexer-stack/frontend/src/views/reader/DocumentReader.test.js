import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import DocumentReader, { CopyAction } from './DocumentReader';
import { readerUrl } from './reader-utils';
import { fetchCaptures } from '../../search/CaptureService';
jest.mock('../ArchiveStatusBar', () => () => <span>Build: test123</span>);
jest.mock('../../search/CaptureService');
const id = 'websites/example.org/20000101000000/path?q=1';
const newer = 'websites/example.org/20010101000000/path?q=1';
const source = {
  title: 'An archived guide', url: 'https://web.archive.org/web/20000101000000/http://example.org/path?q=1',
  alternate_url: 'https://example.org/alternate', capture_date: '2000-01-01T10:00:00Z', llm_guessed_date: '1999-01-01',
  domain_name: 'example.org', text_full: '# Source heading\n\nPreserved text\n', llm_image_text_full: 'Image transcript'
};
const reply = (docId, doc = source) => ({ ok: true, json: async () => ({ hits: { hits: [{ _id: docId, _source: doc }] } }) });
const search = (docId = id, options) => readerUrl(docId, options).slice('/document'.length);
const read = (options, docId = id) => render(<DocumentReader search={search(docId, options)} />);
beforeEach(() => {
  jest.clearAllMocks();
  global.fetch = jest.fn((url, options) => Promise.resolve(reply(JSON.parse(options.body).query.ids.values[0])));
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: jest.fn().mockResolvedValue() } });
  Element.prototype.scrollIntoView = jest.fn();
  URL.createObjectURL = jest.fn(() => 'blob:text');
  URL.revokeObjectURL = jest.fn();
  jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  fetchCaptures.mockResolvedValue({ records: [], nextOffset: null });
});
afterEach(() => { jest.restoreAllMocks(); });

test('reads the exact document with source provenance, find, citation, download and OCR', async () => {
  read({ find: 'Preserved' });
  expect(screen.getByRole('status')).toHaveTextContent('Loading document');
  expect(await screen.findByRole('heading', { name: 'An archived guide' })).toBeVisible();
  expect(screen.getByRole('heading', { name: 'Source heading' })).toBeVisible();
  expect(screen.getByText(/AI estimate; verify/)).toBeVisible();
  expect(screen.getByText(/not necessarily when it was published/)).toBeVisible();
  expect(screen.queryByText('Rediscover early EverQuest.')).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Alternate source ↗' })).toHaveAttribute('href', source.alternate_url);
  expect(screen.getByRole('searchbox')).toHaveValue('Preserved');
  expect(screen.getByText('1 of 1 matches')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Copy citation' }));
  expect(await screen.findByText('Copied.')).toBeVisible();
  expect(navigator.clipboard.writeText.mock.calls[0][0]).toContain('Captured 2000-01-01T10:00:00Z (archive timestamp)');
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: '"literal quotes"' } });
  fireEvent.click(screen.getByRole('button', { name: 'Copy link' }));
  await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(2));
  const link = new URL(navigator.clipboard.writeText.mock.calls[1][0]);
  expect(link.searchParams.get('id')).toBe(id);
  expect(link.searchParams.get('find')).toBe('"literal quotes"');
  fireEvent.click(screen.getByRole('button', { name: 'Download text' }));
  expect(URL.createObjectURL).toHaveBeenCalled();
  fireEvent.change(screen.getByRole('combobox', { name: 'Text to view' }), { target: { value: 'ocr' } });
  expect(screen.getByText('Image transcript')).toBeVisible();
  expect(screen.getByText(/model-generated and may contain errors/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Download text' }));
  expect(URL.createObjectURL).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole('button', { name: 'View captures' }));
  expect(await screen.findByText('No other capture records are available.')).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(1);
});

test('returns to the exact results URL and keeps that route through capture navigation', async () => {
  const returnTo = '/?q=ancient%20cyclops&current=2&sortField=capture_date&filters%5B0%5Bfield%5D=domain_name';
  fetchCaptures.mockResolvedValueOnce({
    records: [{ _meta: { id: newer }, title: { raw: 'Later guide' }, capture_date: { raw: '2001-01-01' }, url: { raw: 'https://web.archive.org/web/20010101000000/http://example.org/path?q=1' } }],
    nextOffset: null
  });
  read({ returnTo, find: 'Preserved' });
  expect(await screen.findByRole('heading', { name: 'An archived guide' })).toBeVisible();
  expect(screen.getByRole('link', { name: '← Back to results' })).toHaveAttribute('href', returnTo);
  fireEvent.click(screen.getByRole('button', { name: 'Copy link' }));
  await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1));
  expect(new URL(navigator.clipboard.writeText.mock.calls[0][0]).searchParams.has('return')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: 'View captures' }));
  const readCapture = await screen.findByRole('link', { name: 'Read capture from 2001-01-01' });
  expect(new URL(readCapture.href).searchParams.get('return')).toBe(returnTo);
  const compare = screen.getByRole('link', { name: 'Compare capture from 2001-01-01 with selected capture' });
  expect(new URL(compare.href).searchParams.get('return')).toBe(returnTo);
});

test.each([false, true])('offers manual copy when clipboard is missing or denied (%s)', async denied => {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: denied ? { writeText: jest.fn().mockRejectedValue(new Error('denied')) } : undefined });
  render(<CopyAction label="Copy citation" text="Exact citation" />);
  fireEvent.click(screen.getByRole('button', { name: 'Copy citation' }));
  expect(await screen.findByRole('status')).toHaveTextContent('Select and copy the text below');
  const fallback = screen.getByRole('textbox', { name: 'Copy citation manually' });
  expect(fallback).toHaveValue('Exact citation');
  fireEvent.focus(fallback);
  expect(fallback.selectionEnd).toBe('Exact citation'.length);
});

test('handles missing IDs without sending a broad search', () => {
  render(<DocumentReader search="" />);
  expect(screen.getByRole('alert')).toHaveTextContent('missing a document ID');
  expect(fetch).not.toHaveBeenCalled();
});

test('retries failed retrievals without leaking upstream details', async () => {
  fetch.mockRejectedValueOnce(new Error('private upstream details'));
  read();
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load this document');
  expect(screen.queryByText(/private upstream/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  expect(await screen.findByRole('heading', { name: 'Source heading' })).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(2);
});

test('labels deleted records and absent source without substituting summaries', async () => {
  fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ hits: { hits: [] } }) });
  const { rerender } = read();
  expect(await screen.findByRole('alert')).toHaveTextContent('no longer available');
  fetch.mockResolvedValueOnce(reply('mail/message', { mailing_list_name: 'EQ list', llm_summary: 'Do not display generated summary' }));
  rerender(<DocumentReader search={search('mail/message')} />);
  expect(await screen.findByText(/No extracted text is available/)).toBeVisible();
  expect(screen.getByRole('heading', { name: 'Untitled document' })).toBeVisible();
  expect(screen.getByText('Date unknown')).toBeVisible();
  expect(screen.getByText('EQ list')).toBeVisible();
  expect(screen.queryByText(/Do not display/)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'View captures' })).not.toBeInTheDocument();
});

test('starts with labelled OCR for image-only sources and supports a direct OCR link', async () => {
  fetch.mockResolvedValueOnce(reply(id, { ...source, text_full: '' }));
  const view = read();
  expect(await screen.findByText('Image transcript')).toBeVisible();
  expect(screen.getByRole('combobox')).toHaveValue('ocr');
  view.unmount();
  read({ part: 'ocr' });
  expect(await screen.findByText('Image transcript')).toBeVisible();
});

test('late document successes and failures cannot replace the newly selected document', async () => {
  let resolveOld, rejectLater;
  fetch.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; }));
  const { rerender, unmount } = read();
  const oldSignal = fetch.mock.calls[0][1].signal;
  fetch.mockResolvedValueOnce(reply(newer, { ...source, title: 'Newer document' }));
  rerender(<DocumentReader search={search(newer)} />);
  expect(await screen.findByRole('heading', { name: 'Newer document' })).toBeVisible();
  expect(oldSignal.aborted).toBe(true);
  await act(async () => resolveOld(reply(id, { ...source, title: 'Obsolete response' })));
  expect(screen.queryByText('Obsolete response')).not.toBeInTheDocument();
  fetch.mockReturnValueOnce(new Promise((resolve, reject) => { rejectLater = reject; }));
  rerender(<DocumentReader search={search('another')} />);
  expect(screen.queryByRole('heading', { name: 'Newer document' })).not.toBeInTheDocument();
  const lastSignal = fetch.mock.calls[2][1].signal;
  unmount();
  expect(lastSignal.aborted).toBe(true);
  await act(async () => rejectLater(new Error('late failure')));
});

test('compares exact dated captures and provides reversible links and full downloads', async () => {
  fetch.mockImplementation((url, options) => {
    const selected = JSON.parse(options.body).query.ids.values[0];
    return Promise.resolve(reply(selected, selected === id ? source : { ...source, title: 'Later guide', capture_date: '2001-01-01', text_full: '# Source heading\n\nChanged text\n' }));
  });
  read({ compare: newer });
  expect(await screen.findByText('1 added · 1 removed lines')).toBeVisible();
  expect(screen.getByRole('heading', { name: 'Compare captures' })).toBeVisible();
  expect(within(screen.getByRole('region', { name: 'From' })).getByText('2000-01-01T10:00:00Z')).toBeVisible();
  expect(within(screen.getByRole('region', { name: 'To' })).getByText('2001-01-01')).toBeVisible();
  const swap = new URL(screen.getByRole('link', { name: 'Swap captures' }).href);
  expect(swap.searchParams.get('id')).toBe(newer);
  expect(swap.searchParams.get('compare')).toBe(id);
  expect(screen.getAllByRole('link', { name: 'Read this capture' })).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: 'Download from text' }));
  fireEvent.click(screen.getByRole('button', { name: 'Download to text' }));
  expect(URL.createObjectURL).toHaveBeenCalledTimes(2);
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'ocr' } });
  expect(await screen.findByText('The image transcription (OCR) is identical in these two captures.')).toBeVisible();
  expect(screen.getByRole('link', { name: 'Swap captures' })).toHaveAttribute('href', expect.stringContaining('part=ocr'));
});

test.each(['from', 'to', 'both'])('does not present missing %s source text as additions or deletions', async missing => {
  fetch.mockImplementation((url, options) => {
    const selected = JSON.parse(options.body).query.ids.values[0];
    const empty = missing === 'both' || (missing === 'from' ? selected === id : selected === newer);
    return Promise.resolve(reply(selected, { ...source, text_full: empty ? ' ' : 'Original\n' }));
  });
  read({ compare: newer });
  expect(await screen.findByText(/Missing text cannot establish an addition or deletion/)).toBeVisible();
  expect(screen.queryByRole('region', { name: 'Text changes' })).not.toBeInTheDocument();
});

test('rejects self-comparisons and records from different pages', async () => {
  const view = read({ compare: id });
  expect(await screen.findByText('Choose two different captures to compare.')).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(1);
  view.unmount();
  fetch.mockImplementation((url, options) => {
    const selected = JSON.parse(options.body).query.ids.values[0];
    return Promise.resolve(reply(selected, selected === id ? source : { ...source, url: 'https://web.archive.org/web/20010101000000/http://other.org/page' }));
  });
  read({ compare: newer });
  expect(await screen.findByText(/not captures of the same original page/)).toBeVisible();
  expect(screen.queryByRole('region', { name: 'Text changes' })).not.toBeInTheDocument();
});

test('retries a comparison capture independently and never displays a previous comparison body', async () => {
  fetch.mockImplementation((url, options) => {
    const selected = JSON.parse(options.body).query.ids.values[0];
    return selected === id ? Promise.resolve(reply(id)) : Promise.reject(new Error('unavailable'));
  });
  read({ compare: newer });
  expect(await screen.findByRole('alert')).toHaveTextContent('Comparison capture: Could not load');
  fetch.mockResolvedValueOnce(reply(newer));
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  expect(await screen.findByText('The extracted text is identical in these two captures.')).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(3);
});
