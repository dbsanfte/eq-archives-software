import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import DiscussionResult from './DiscussionResult';
import { ReaderSearchContext } from '../reader/reader-utils';

const match = i => ({ _meta: { id: `capture-${i}` }, capture_date: { raw: '2006-12-13T16:00:00Z' }, text_full: { snippet: `Passage <em>${i}</em> &amp; detail` } });
const result = { ...match(0), domain_name: { raw: 'forum.example.org' }, _discussionGroup: {
  key: 'discussion:abc', title: 'A verified discussion', forum: '/eq/Veterans', matches: [0, 1, 2, 3].map(match)
} };

test('shows one source excerpt and a single matches disclosure with exact reader links', () => {
  render(<ReaderSearchContext.Provider value="zone"><DiscussionResult result={result} returnTo="/?q=zone&current=2" /></ReaderSearchContext.Provider>);
  expect(screen.getByRole('link', { name: 'Read discussion' })).toHaveAttribute('href', expect.stringContaining('id=capture-0'));
  expect(screen.queryByRole('region', { name: 'Matching excerpts' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Show matches' }));
  const links = screen.getAllByRole('link', { name: /Read from here/ });
  expect(links).toHaveLength(3);
  const url = new URL(links[1].href);
  expect(url.searchParams.get('id')).toBe('capture-1');
  expect(url.searchParams.get('find')).toBe('Passage 1 & detail');
  expect(url.searchParams.get('return')).toBe('/?q=zone&current=2');
  fireEvent.click(screen.getByRole('button', { name: 'Show more matches' }));
  expect(screen.getAllByRole('link', { name: /Read from here/ })).toHaveLength(4);
  expect(screen.queryByRole('button', { name: 'Show more matches' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Hide matches' }));
  expect(screen.queryByRole('region', { name: 'Matching excerpts' })).not.toBeInTheDocument();
});

test('keeps unknown dates and missing snippets explicit without treating them as matching post identities', () => {
  const item = { _meta: { id: 'no-snippet' } };
  render(<DiscussionResult result={{ ...result, domain_name: undefined, _discussionGroup: { ...result._discussionGroup, matches: [item] } }} />);
  fireEvent.click(screen.getByRole('button', { name: 'Show matches' }));
  expect(screen.getByText('Open this captured page to read the matching content.')).toBeVisible();
  expect(screen.getByRole('link', { name: /Read from here/ })).toHaveTextContent('date unknown');
});
