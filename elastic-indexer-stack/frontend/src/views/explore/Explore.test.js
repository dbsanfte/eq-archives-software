import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import Explore from './Explore';
import { fetchExplore, DEFAULT_SELECTION } from './ExploreService';
import SiteSearchScope, { websiteScope } from '../search/SiteSearchScope';

jest.mock('./ExploreService', () => ({ ...jest.requireActual('./ExploreService'), fetchExplore: jest.fn() }));
jest.mock('../ArchiveStatusBar', () => () => <span>Archive status</span>);
const overview = selection => ({ selection, generated_at: '2026-10-10T12:00:00Z', records: 12000, sites_count: 20, tagged: 6000,
  timeline: [{ year: 2000, records: 10000, sites: 18 }, { year: 2001, records: 0, sites: 0 }],
  themes: [{ key: 'raid', count: 250, approximate: false }, { key: 'lore', count: 50, approximate: true }],
  sites: [{ key: 'guild.org:8080', count: 200, approximate: false }, { key: 'other.org', count: 100, approximate: true }] });
const phrases = selection => ({ selection, generated_at: '2026-10-10T12:00:00Z', sampled_pages: 50, sampled_sites: 12,
  duplicate_captures: 10, clipped_pages: 3, excerpt_chars: 12000, sample_limit: 100, vocabulary_limited: true,
  phrases: [{ text: 'ancient cyclops', pages: 20 }, { text: 'cleric', pages: 10 }] });
const reply = (kind, selection) => Promise.resolve(kind === 'overview' ? overview(selection) : phrases(selection));
const ready = () => screen.findByRole('button', { name: 'ancient cyclops: 20 sampled pages' });

beforeEach(() => {
  window.history.replaceState({}, '', '/explore');
  fetchExplore.mockReset().mockImplementation(reply);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: jest.fn().mockResolvedValue() } });
});
afterEach(() => { jest.useRealTimers(); window.history.replaceState({}, '', '/'); });

test('links timeline, themes, source phrases and sites and keeps a native search URL', async () => {
  render(<Explore />);
  expect(screen.getByText('Loading charts…')).toBeVisible();
  await ready();
  expect(screen.getByText(/Distinctive source terms, with everyday English filtered out/)).toBeVisible();
  expect(screen.getByRole('link', { name: 'wordfreq by Robyn Speer', hidden: true })).toHaveAttribute('href', 'https://github.com/rspeer/wordfreq');
  expect(screen.getByRole('link', { name: 'Explore', exact: true })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByText('12,000')).toBeVisible();
  expect(screen.getByText('50%')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Site count', exact: true }));
  fireEvent.click(screen.getByRole('button', { name: /2000: 18 estimated domains/ }));
  await waitFor(() => expect(fetchExplore).toHaveBeenLastCalledWith('phrases', expect.objectContaining({ start: '2000-01-01', end: '2000-12-31' }), expect.anything()));
  await ready();
  fireEvent.click(within(screen.getByRole('list', { name: 'Themes' })).getByRole('button', { name: /raid/ }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove theme: raid' })).toBeVisible());
  await ready();
  fireEvent.click(screen.getByRole('button', { name: 'ancient cyclops: 20 sampled pages' }));
  await ready();
  fireEvent.click(within(screen.getByRole('list', { name: 'Contributing sites' })).getByRole('button', { name: /guild.org:8080/ }));
  await ready();
  expect(window.location.search).toContain('site=guild.org%3A8080');
  const url = new URL(screen.getByRole('link', { name: /Search these pages/ }).href);
  expect(url.searchParams.get('q')).toBe('text_full:"ancient cyclops"');
  expect(url.searchParams.get('filters[2][values][0]')).toBe('raid');
  expect(url.searchParams.get('filters[3][values][0]')).toBe('guild.org:8080');
  fireEvent.click(screen.getByRole('button', { name: 'Remove phrase: ancient cyclops' }));
  await ready();
  fireEvent.click(screen.getByRole('button', { name: 'Reset exploration' }));
  await ready();
  expect(screen.queryByRole('button', { name: /Remove (theme|site|phrase)/ })).not.toBeInTheDocument();
});

test('dates apply explicitly; presets and browser history update dates without losing unrelated drafts on refresh', async () => {
  render(<Explore />); await ready();
  const from = screen.getByLabelText('From');
  fireEvent.change(from, { target: { value: '2000-02-29' } });
  fireEvent.change(screen.getByLabelText('Date meaning'), { target: { value: 'llm_guessed_date' } });
  const calls = fetchExplore.mock.calls.length;
  expect(calls).toBe(2);
  fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
  await ready();
  expect(from).toHaveValue('2000-02-29');
  expect(screen.getByLabelText('Date meaning')).toHaveValue('llm_guessed_date');
  fireEvent.click(screen.getByRole('button', { name: 'Apply dates' }));
  await ready();
  expect(screen.getByText(/Publication dates are AI estimates/)).toBeVisible();
  fireEvent.change(from, { target: { value: '2007-01-01' } });
  expect(screen.getByRole('button', { name: 'Apply dates' })).toBeDisabled();
  fireEvent.submit(screen.getByRole('form', { name: 'Explore dates' }));
  expect(window.location.search).toContain('start=2000-02-29');
  fireEvent.change(screen.getByLabelText('Through'), { target: { value: '2008-01-01' } });
  expect(screen.getByRole('button', { name: 'Apply dates' })).toBeEnabled();
  for (const preset of ['1999–2001', '2002–2006', '1999–2006']) {
    fireEvent.click(screen.getByRole('button', { name: preset, exact: true })); await ready();
  }
  act(() => { window.history.replaceState({}, '', '/explore?start=2001-01-01&end=2002-12-31'); window.dispatchEvent(new PopStateEvent('popstate')); });
  await ready();
  expect(from).toHaveValue('2001-01-01');
  expect(screen.getByLabelText('Date meaning')).toHaveValue('capture_date');
});

test('cloud/list modes, toggled selections, sharing and clipboard fallback are accessible', async () => {
  render(<Explore />); await ready();
  fireEvent.click(screen.getByRole('button', { name: 'List', exact: true }));
  expect(screen.getByText('20 pages')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Cloud', exact: true }));
  expect(screen.queryByText('20 pages')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Captures', exact: true }));
  for (const list of ['Themes', 'Contributing sites']) {
    for (let n = 0; n < 2; n++) {
      fireEvent.click(within(screen.getByRole('list', { name: list })).getAllByRole('button')[0]); await ready();
    }
  }
  for (let n = 0; n < 2; n++) { fireEvent.click(screen.getByRole('button', { name: 'ancient cyclops: 20 sampled pages' })); await ready(); }
  fireEvent.click(screen.getByRole('button', { name: 'Share selection' }));
  expect(await screen.findByText('Link copied.')).toBeVisible();
  navigator.clipboard.writeText.mockRejectedValue(new Error('denied'));
  fireEvent.click(screen.getByRole('button', { name: 'Share selection' }));
  const input = await screen.findByLabelText('Copy this link');
  expect(input.value).toContain('/explore?start=1999-01-01');
  fireEvent.focus(input);
  expect(input.selectionEnd).toBe(input.value.length);
});

test('cloud separates close frequencies, keeps ties equal, and exposes unchanged page counts', async () => {
  let terms = [{ text: 'ancient cyclops', pages: 20 }, { text: 'cleric', pages: 19 }, { text: 'necromancer', pages: 20 }];
  fetchExplore.mockImplementation((kind, selection) => Promise.resolve(kind === 'overview' ? overview(selection)
    : { ...phrases(selection), phrases: terms }));
  render(<Explore />); await ready();
  const buttons = () => within(screen.getByRole('list', { name: 'Source phrases' })).getAllByRole('button');
  expect(parseFloat(buttons()[0].style.fontSize) / parseFloat(buttons()[1].style.fontSize)).toBeGreaterThan(2.4);
  expect(buttons()[0].style.fontSize).toBe(buttons()[2].style.fontSize);
  expect(screen.getByText(/Relative frequency: 19–20 sampled pages/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'List', exact: true }));
  expect(screen.getAllByText('20 pages')).toHaveLength(2);
  expect(screen.getByText('19 pages')).toBeVisible();
  expect(buttons()[1]).toHaveAccessibleName('cleric: 19 sampled pages');
  fireEvent.click(screen.getByRole('button', { name: 'Cloud', exact: true }));
  terms = terms.map(term => ({ ...term, pages: 20 }));
  fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
  expect(await screen.findByText(/Equal frequency: every term appears on 20 sampled pages/)).toBeVisible();
  expect(new Set(buttons().map(button => button.style.fontSize)).size).toBe(1);
  terms = [{ text: 'ancient cyclops', pages: 1 }];
  fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
  expect(await screen.findByText(/every term appears on 1 sampled page\./)).toBeVisible();
});

test('shows invalid links, empty charts and missing phrase evidence honestly', async () => {
  window.history.replaceState({}, '', '/explore?start=invalid');
  fetchExplore.mockImplementation((kind, selection) => Promise.resolve(kind === 'overview' ? {
    ...overview(selection), records: 0, tagged: 0, sites_count: 0, timeline: [], sites: [], themes: []
  } : { ...phrases(selection), phrases: [], vocabulary_limited: false }));
  render(<Explore />);
  expect(screen.getByRole('alert')).toHaveTextContent('invalid filters');
  expect(await screen.findByText(/No indexed website captures/)).toBeVisible();
  expect(screen.getByText(/No distinctive source phrases/)).toBeVisible();
  expect(screen.getByText(/No theme tags/)).toBeVisible();
  expect(screen.getByText(/No sites match/)).toBeVisible();
});

test('keeps successful charts when phrase loading fails and retries independently', async () => {
  fetchExplore.mockImplementation((kind, selection) => kind === 'phrases' ? Promise.reject(new Error('offline')) : reply(kind, selection));
  render(<Explore />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not update phrases');
  expect(screen.getByText('12,000')).toBeVisible();
  fetchExplore.mockImplementation(reply);
  fireEvent.click(screen.getByRole('button', { name: 'Retry phrases' })); await ready();
  fetchExplore.mockRejectedValue(new Error('offline'));
  fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }));
  expect((await screen.findAllByRole('alert'))[0]).toHaveTextContent('previous snapshot');
  fetchExplore.mockImplementation(reply);
  fireEvent.click(screen.getByRole('button', { name: 'Retry charts' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry charts' })).not.toBeInTheDocument());
});

test('late results/errors cannot replace a newer selection; unmount cancels in-flight reads', async () => {
  const pending = [];
  fetchExplore.mockImplementation((kind, selection, signal) => new Promise((resolve, reject) => pending.push({ kind, selection, signal, resolve, reject })));
  const { unmount } = render(<Explore />);
  fireEvent.click(screen.getByRole('button', { name: '1999–2001', exact: true }));
  expect(pending[0].signal.aborted).toBe(true);
  await act(async () => {
    pending[2].resolve(overview(pending[2].selection)); pending[3].resolve(phrases(pending[3].selection));
  });
  await ready();
  await act(async () => { pending[0].resolve({ ...overview(pending[0].selection), records: 777 }); pending[1].reject(new Error('old')); });
  expect(screen.queryByText('777')).not.toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '2002–2006', exact: true }));
  expect(screen.queryByText('12,000')).not.toBeInTheDocument();
  unmount();
  expect(pending.at(-1).signal.aborted).toBe(true);
  await act(async () => pending.at(-1).resolve(phrases(DEFAULT_SELECTION)));
});

test('timeouts offer explicit retry', async () => {
  jest.useFakeTimers();
  fetchExplore.mockImplementation((kind, selection, signal) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('timeout')))));
  render(<Explore />);
  await act(async () => jest.advanceTimersByTime(25000));
  expect(screen.getByRole('button', { name: 'Retry charts' })).toBeVisible();
});

test('website collection scope stays visible and removable in search', () => {
  expect(websiteScope()).toBe(false);
  expect(websiteScope([{ field: 'id', type: 'range' }])).toBe(false);
  const removeFilter = jest.fn();
  render(<SiteSearchScope filters={[{ field: 'id', type: 'range', values: [{ from: 'websites/', to: 'websites0' }] }]} removeFilter={removeFilter} />);
  expect(screen.getByRole('heading', { name: 'Archived websites' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Include other collections' }));
  expect(removeFilter).toHaveBeenCalledWith('id');
});


test('phone chart navigation changes the selected panel without changing filters or making requests', async () => {
  const oldWidth = window.innerWidth;
  window.innerWidth = 390;
  try {
    render(<Explore />); await ready();
    expect(document.querySelector('.explore-date-settings')).not.toHaveAttribute('open');
    const calls = fetchExplore.mock.calls.length;
    const scroll = jest.fn();
    document.getElementById('explore-words').scrollIntoView = scroll;
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Explore charts' })).getByRole('button', { name: 'Words' }));
    expect(document.getElementById('explore-words')).toHaveAttribute('data-selected', 'true');
    expect(document.getElementById('explore-timeline')).toHaveAttribute('data-selected', 'false');
    expect(fetchExplore).toHaveBeenCalledTimes(calls);
    expect(scroll).toHaveBeenCalledWith({ block: 'start' });
  } finally { window.innerWidth = oldWidth; }
});
