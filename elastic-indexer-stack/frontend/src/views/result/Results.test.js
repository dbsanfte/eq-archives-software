import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { useMediaQuery } from '@mui/material';
import CustomResultView from './CustomResultView';
import ResultDisplayContext from './ResultDisplayContext';
import ButtonRow from './ButtonRow';
import LabelRow from './LabelRow';
import TagRow from './TagRow';
import SyntaxExamples from '../search/SyntaxExamples';

jest.mock('@mui/material', () => ({
  ...jest.requireActual('@mui/material'), useMediaQuery: jest.fn()
}));

const result = {
  id: { raw: 'archive/id with spaces' }, title: { raw: 'An archived guide' },
  url: { raw: 'https://example.org/guide' }, alternate_url: { raw: 'https://mirror.example/guide' },
  thumbnail: { raw: '/thumbnails/website.webp' },
  llm_summary: { raw: '<p>A useful summary</p>' },
  text_full: { raw: '# Full archived text', snippet: '<em>A matching passage</em>' },
  llm_content_flavour: { raw: 'Guide' }, mailing_list_name: { raw: 'EverQuest' },
  domain_name: { raw: 'example.org' }, capture_date: { raw: '2000-01-01T00:00:00Z' },
  llm_guessed_date: { raw: '1999' }, llm_tags: { raw: ['Wizard', 'Cleric'] }
};

beforeEach(() => {
  useMediaQuery.mockReturnValue(false);
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true, value: { writeText: jest.fn().mockResolvedValue(undefined) }
  });
  jest.spyOn(window, 'open').mockImplementation(() => {});
  jest.spyOn(window, 'alert').mockImplementation(() => {});
});
afterEach(() => jest.restoreAllMocks());

test('renders source evidence before a collapsed AI summary, with full detail available', () => {
  const onClickLink = jest.fn(event => event.preventDefault());
  render(<CustomResultView result={result} onClickLink={onClickLink} />);
  expect(screen.getByRole('link', { name: 'An archived guide' })).toHaveAttribute('href', result.url.raw);
  fireEvent.click(screen.getByRole('link', { name: 'An archived guide' }));
  expect(onClickLink).toHaveBeenCalledTimes(1);
  expect(screen.getByText('A matching passage')).toBeInTheDocument();
  const summary = screen.getByText('AI-generated summary').closest('details');
  expect(summary).not.toHaveAttribute('open');
  expect(screen.getByText('A matching passage').compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  fireEvent.click(screen.getByText('AI-generated summary'));
  expect(summary).toHaveAttribute('open');
  expect(screen.getByText('A useful summary')).toBeInTheDocument();
  expect(screen.getByText('Captured: 2000-01-01')).toBeInTheDocument();
  expect(screen.getByText('Estimated: 1999')).toBeInTheDocument();
  const tags = [screen.getByText('Cleric'), screen.getByText('Wizard')];
  expect(tags[0].compareDocumentPosition(tags[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test('detailed cards display the complete AI summary after the source excerpt', () => {
  render(<ResultDisplayContext.Provider value={false}><CustomResultView result={result} /></ResultDisplayContext.Provider>);
  expect(screen.getByRole('heading', { name: 'AI-generated summary' }).closest('details')).toBeNull();
  expect(screen.getByText('A useful summary')).toBeVisible();
  expect(screen.getByText('A matching passage').compareDocumentPosition(screen.getByRole('heading', { name: 'AI-generated summary' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test('renders incomplete archive metadata without an empty snippet section', () => {
  render(<CustomResultView result={{
    id: { raw: 'minimal' }, title: { raw: 'Minimal record' },
    url: { raw: 'https://example.org/' }, thumbnail: { raw: '/thumbnail.webp' }
  }} />);
  expect(screen.getByRole('link', { name: 'Minimal record' })).toBeInTheDocument();
  expect(screen.queryByText('From the archive')).not.toBeInTheDocument();
  expect(screen.queryByText('AI-generated summary')).not.toBeInTheDocument();
  expect(screen.getByText('Open the full text to explore this record.')).toBeInTheDocument();
});

test.each(['<em>A matching passage</em>', ''])('keeps pending summaries out of cards while retaining available source text: %s', snippet => {
  render(<CustomResultView result={{
    ...result,
    llm_summary: { raw: '[ Still awaiting LLM Enrichment... ]' },
    text_full: { raw: '# Full archived text', snippet }
  }} />);
  expect(screen.queryByText(/awaiting LLM/)).not.toBeInTheDocument();
  expect(screen.queryByText('AI-generated summary')).not.toBeInTheDocument();
  if (snippet) {
    expect(screen.getByText('A matching passage')).toBeInTheDocument();
    expect(screen.queryByText('Open the full text to explore this record.')).not.toBeInTheDocument();
  } else {
    expect(screen.getByText('Open the full text to explore this record.')).toBeInTheDocument();
  }
  expect(screen.getByRole('link', { name: 'Read document' })).toHaveAttribute('href', '/document?id=archive%2Fid+with+spaces');
});

test('omits blank labels and safely handles missing tag lists', () => {
  render(<LabelRow flavour=" " mailingList=" " domain=" " captureDate=" " guessedDate=" " />);
  expect(screen.queryByText(/Captured:/)).not.toBeInTheDocument();
  const log = jest.spyOn(console, 'error').mockImplementation(() => {});
  render(<TagRow tags={undefined} />);
  expect(screen.queryByText('Cleric')).not.toBeInTheDocument();
  log.mockRestore();
});

test('opens alternate source links from the result actions', () => {
  render(<ButtonRow result={result} />);
  expect(screen.getByRole('link', { name: 'Read document' })).toHaveAttribute('href', '/document?id=archive%2Fid+with+spaces');
  fireEvent.click(screen.getByRole('button', { name: 'Alternate Link' }));
  expect(window.open).toHaveBeenCalledWith(result.alternate_url.raw, '_blank', 'noopener,noreferrer');
});

test('copies an encoded permalink and dismisses the confirmation', async () => {
  jest.useFakeTimers();
  try {
    render(<ButtonRow result={result} />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy Permalink' })); });
    const copied = new URL(navigator.clipboard.writeText.mock.calls[0][0]);
    expect(copied.origin).toBe(window.location.origin);
    expect(copied.searchParams.get('id')).toBe(result.id.raw);
    expect(screen.getByText('Permalink copied to clipboard')).toBeInTheDocument();
    act(() => jest.advanceTimersByTime(4000));
    act(() => jest.advanceTimersByTime(500));
    expect(screen.queryByText('Permalink copied to clipboard')).not.toBeInTheDocument();
  } finally { jest.useRealTimers(); }
});

test('reader navigation retains the results URL but copied permalinks stay stable', async () => {
  const returnTo = '/?q=ancient%20cyclops&current=2&sortField=capture_date&filters%5B0%5D%5Bfield%5D=domain_name';
  render(<ButtonRow result={result} returnTo={returnTo} />);
  const reader = new URL(screen.getByRole('link', { name: 'Read document' }).href);
  expect(reader.searchParams.get('return')).toBe(returnTo);
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Copy Permalink' })));
  const copied = new URL(navigator.clipboard.writeText.mock.calls[0][0]);
  expect(copied.searchParams.get('id')).toBe(result.id.raw);
  expect(copied.searchParams.has('return')).toBe(false);
});

test('handles missing optional links, missing IDs, and denied clipboard access', async () => {
  const error = new Error('Clipboard denied');
  navigator.clipboard.writeText.mockRejectedValue(error);
  const log = jest.spyOn(console, 'error').mockImplementation(() => {});
  render(<ButtonRow result={{}} />);
  fireEvent.click(screen.getByRole('button', { name: 'Alternate Link' }));
  expect(window.open).not.toHaveBeenCalled();
  expect(screen.queryByRole('link', { name: 'Read document' })).not.toBeInTheDocument();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy Permalink' })); });
  expect(window.alert).toHaveBeenCalledWith('Failed to copy permalink to clipboard');
  expect(log).toHaveBeenCalledWith('Failed to copy URL: ', error);
});

test.each([false, true])('keeps query syntax examples available on mobile=%s', mobile => {
  useMediaQuery.mockReturnValue(mobile);
  render(<SyntaxExamples />);
  expect(screen.getByText('Syntax Examples')).toBeInTheDocument();
  expect(screen.getByText('Date range filtering:')).toBeInTheDocument();
  expect(screen.getByRole('link')).toHaveAttribute('href', expect.stringContaining('query-string-syntax'));
});

test('reader and copy links use the real ES identity, including reserved characters', async () => {
  const exact = 'actual/id?# ü%';
  render(<ButtonRow result={{ ...result, _meta: { id: exact } }} />);
  const link = new URL(screen.getByRole('link', { name: 'Read document' }).href);
  expect(link.pathname).toBe('/document');
  expect(link.searchParams.get('id')).toBe(exact);
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Copy Permalink' })));
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(link.href);
});


test('search results use the reader without a duplicate full-text preview button', () => {
  render(<CustomResultView result={result} />);
  expect(screen.getByRole('link', { name: 'Read document' })).toHaveAttribute('href', '/document?id=archive%2Fid+with+spaces');
  expect(screen.queryByRole('button', { name: 'Preview Full Text' })).not.toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
