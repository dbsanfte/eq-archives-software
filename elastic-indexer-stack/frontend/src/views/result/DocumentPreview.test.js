import React, { useState } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import DocumentPreview from './DocumentPreview';

// Capture history still offers previews; exercise their lifecycle independently
// of the result actions, which now link directly to the reader.
function PreviewHarness({ result }) {
  const [open, setOpen] = useState(false);
  return <><button onClick={() => setOpen(true)}>Preview capture</button>
    <DocumentPreview open={open} onClose={() => setOpen(false)} result={result} />
  </>;
}

const result = { id: { raw: 'display-id' }, _meta: { id: 'archive/a file?#.txt' } };
const reply = (text = '# Full source\n\nPreserved archive text', id = result._meta.id) => ({
  ok: true, json: async () => ({ hits: { hits: [{ _id: id, _source: { text_full: text } }] } })
});
const open = () => fireEvent.click(screen.getByRole('button', { name: 'Preview capture' }));
const close = async () => {
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
};

beforeEach(() => { global.fetch = jest.fn().mockResolvedValue(reply()); });
afterEach(() => jest.restoreAllMocks());

test('fetches only the selected full document on demand and reuses it when reopened', async () => {
  let resolve;
  fetch.mockReturnValueOnce(new Promise(done => { resolve = done; }));
  render(<PreviewHarness result={result} />);
  expect(fetch).not.toHaveBeenCalled();
  open();
  expect(screen.getByRole('status')).toHaveTextContent('Loading full text');
  expect(fetch).toHaveBeenCalledTimes(1);
  const [url, options] = fetch.mock.calls[0];
  expect(url).toBe('/elasticsearch/eq-archive/_search');
  expect(options.method).toBe('POST');
  expect(JSON.parse(options.body)).toEqual({ size: 1, _source: ['text_full'], query: { ids: { values: [result._meta.id] } } });
  await act(async () => { resolve(reply()); });
  expect(await screen.findByRole('heading', { name: 'Full source' })).toBeVisible();
  expect(screen.getByText('Preserved archive text')).toBeVisible();
  await close();
  open();
  expect(screen.getByRole('heading', { name: 'Full source' })).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(1);
});

test('offers a safe retry after failure without displaying upstream error text', async () => {
  fetch.mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({ secret: 'upstream credentials' }) });
  render(<PreviewHarness result={result} />);
  open();
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the full text');
  expect(screen.queryByText(/upstream credentials/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
  expect(await screen.findByRole('heading', { name: 'Full source' })).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(2);
});

test.each([[[]], [[{ _id: 'a different record', _source: { text_full: 'Wrong source' } }]]])('rejects missing or mismatched documents', async hits => {
  fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ hits: { hits } }) });
  render(<PreviewHarness result={result} />);
  open();
  expect(await screen.findByRole('alert')).toHaveTextContent('This record is no longer available');
  expect(screen.queryByText('Wrong source')).not.toBeInTheDocument();
});

test('shows a useful empty state and supports the legacy result ID', async () => {
  fetch.mockResolvedValueOnce(reply(null, 'legacy'));
  render(<PreviewHarness result={{ id: { raw: 'legacy' } }} />);
  open();
  expect(await screen.findByText('No full text is available for this record.')).toBeVisible();
  expect(JSON.parse(fetch.mock.calls[0][1].body).query.ids.values).toEqual(['legacy']);
});

test('does not send an unscoped request when the document has no ID', async () => {
  render(<PreviewHarness result={{}} />);
  open();
  expect(await screen.findByRole('alert')).toHaveTextContent('This record is no longer available');
  expect(fetch).not.toHaveBeenCalled();
});

test('aborts closed previews and ignores their delayed responses', async () => {
  let resolve;
  fetch.mockReturnValueOnce(new Promise(done => { resolve = done; }));
  const mounted = render(<PreviewHarness result={result} />);
  open();
  const signal = fetch.mock.calls[0][1].signal;
  await close();
  expect(signal.aborted).toBe(true);
  open();
  expect(await screen.findByRole('heading', { name: 'Full source' })).toBeVisible();
  await act(async () => { resolve(reply('# Obsolete response')); });
  expect(screen.queryByRole('heading', { name: 'Obsolete response' })).not.toBeInTheDocument();
  mounted.unmount();
});

test('cannot show a previous document after the result changes', async () => {
  const mounted = render(<PreviewHarness result={result} />);
  open();
  expect(await screen.findByRole('heading', { name: 'Full source' })).toBeVisible();
  fetch.mockResolvedValueOnce(reply('# Next document', 'next'));
  mounted.rerender(<PreviewHarness result={{ _meta: { id: 'next' } }} />);
  expect(await screen.findByRole('heading', { name: 'Next document' })).toBeVisible();
  expect(screen.queryByRole('heading', { name: 'Full source' })).not.toBeInTheDocument();
});


test.each(['# Legacy source', ''])('supports inline source without making a request: %s', async text => {
  render(<PreviewHarness result={{ ...result, text_full: { raw: text } }} />);
  open();
  if (text) expect(screen.getByRole('heading', { name: 'Legacy source' })).toBeVisible();
  else expect(screen.getByText('No full text is available for this record.')).toBeVisible();
  expect(fetch).not.toHaveBeenCalled();
  await close();
});
