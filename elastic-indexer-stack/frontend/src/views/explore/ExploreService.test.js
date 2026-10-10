import { DEFAULT_SELECTION, validSelection, readSelection, selectionParams, exploreSearchUrl, fetchExplore } from './ExploreService';

export const overview = selection => ({
  selection, generated_at: '2026-10-10T12:00:00Z', records: 12000, sites_count: 20, tagged: 6000,
  timeline: [{ year: 2000, records: 10000, sites: 18 }, { year: 2001, records: 2000, sites: 7 }],
  themes: [{ key: 'raid', count: 250, approximate: false }, { key: 'lore', count: 50, approximate: true }],
  sites: [{ key: 'guild.org:8080', count: 200, approximate: false }, { key: 'other.org', count: 100, approximate: true }]
});
export const phrases = selection => ({
  selection, generated_at: '2026-10-10T12:00:00Z', sampled_pages: 50, sampled_sites: 12,
  examined_captures: 60, duplicate_captures: 10, clipped_pages: 3, excerpt_chars: 12000, sample_limit: 100,
  phrases: [{ text: 'ancient cyclops', pages: 20 }, { text: 'cleric', pages: 10 }]
});

beforeEach(() => { global.fetch = jest.fn(); });

test('round trips defaults, UTC leap days, exact ports and filters in a shareable selection', () => {
  expect(readSelection('')).toEqual({ selection: DEFAULT_SELECTION, invalid: false });
  const selection = { ...DEFAULT_SELECTION, start: '2000-02-29', basis: 'llm_guessed_date', theme: 'class-balance', site: 'www.guild.org:8080', phrase: 'ancient cyclops' };
  expect(readSelection(`?${selectionParams(selection)}`)).toEqual({ selection, invalid: false });
});

test.each([
  { start: '2001-02-29' }, { start: '' }, { end: '2000-13-01' }, { start: '2007-01-01' },
  { start: '1989-12-31' }, { end: '2100-01-01' }, { end: '2049-12-31' }, { basis: 'last_indexed' },
  { site: 'x'.repeat(256) }, { theme: '\nraid' }, { phrase: 3 }
])('rejects invalid selection %j without querying', patch => {
  expect(validSelection({ ...DEFAULT_SELECTION, ...patch })).toBe(false);
});

test.each(['?unknown=x', '?start=2000-01-01&start=2001-01-01', '?start=bogus'])('labels invalid links and safely falls back: %s', query => {
  expect(readSelection(query)).toEqual({ selection: DEFAULT_SELECTION, invalid: true });
});

test('search links preserve exact date, collection, site and theme filters and source phrase', () => {
  const params = new URLSearchParams(exploreSearchUrl({ ...DEFAULT_SELECTION, start: '2000-02-29',
    basis: 'llm_guessed_date', theme: 'class-balance', site: 'www.guild.org:8080', phrase: 'ancient "cyclops"' }).slice(2));
  expect(params.get('q')).toBe('text_full:"ancient  cyclops "');
  expect(params.get('filters[0][field]')).toBe('llm_guessed_date');
  expect(params.get('filters[0][values][0][from]')).toBe('2000-02-29T00:00:00.000Z');
  expect(params.get('filters[0][values][0][to]')).toBe('2006-12-31T23:59:59.999Z');
  expect(params.get('filters[1][values][0][from]')).toBe('websites/');
  expect(params.get('filters[2][values][0]')).toBe('class-balance');
  expect(params.get('filters[3][values][0]')).toBe('www.guild.org:8080');
  expect(new URLSearchParams(exploreSearchUrl(DEFAULT_SELECTION).slice(2)).has('q')).toBe(false);
});

test.each([['overview', overview], ['phrases', phrases]])('validates and fetches %s with cancellation', async (kind, fixture) => {
  const data = fixture(DEFAULT_SELECTION);
  fetch.mockResolvedValue({ ok: true, json: async () => data });
  const signal = new AbortController().signal;
  expect(await fetchExplore(kind, DEFAULT_SELECTION, signal)).toEqual(data);
  expect(fetch).toHaveBeenCalledWith(`/api/explore/${kind}?${selectionParams(DEFAULT_SELECTION)}`, { signal });
});

test.each([
  ['overview', () => ({ generated_at: 'x' })],
  ['overview', () => ({ ...overview(DEFAULT_SELECTION), selection: null })],
  ['overview', () => overview({ ...DEFAULT_SELECTION, theme: 'stale' })],
  ['overview', () => ({ ...overview(DEFAULT_SELECTION), records: -1 })],
  ['overview', () => ({ ...overview(DEFAULT_SELECTION), themes: null })],
  ['overview', () => ({ ...overview(DEFAULT_SELECTION), sites: [{ key: '', count: 2 }] })],
  ['overview', () => ({ ...overview(DEFAULT_SELECTION), timeline: null })],
  ['overview', () => ({ ...overview(DEFAULT_SELECTION), timeline: [{ year: 2100, records: 1, sites: 1 }] })],
  ['phrases', () => ({ ...phrases(DEFAULT_SELECTION), sampled_pages: -1 })],
  ['phrases', () => ({ ...phrases(DEFAULT_SELECTION), phrases: null })],
  ['phrases', () => ({ ...phrases(DEFAULT_SELECTION), phrases: [{ text: 'x'.repeat(101), pages: 2 }] })],
])('rejects incomplete/mismatched %s data', async (kind, fixture) => {
  fetch.mockResolvedValue({ ok: true, json: async () => fixture() });
  await expect(fetchExplore(kind, DEFAULT_SELECTION)).rejects.toThrow();
});

test('surfaces HTTP and parsing failures', async () => {
  fetch.mockResolvedValue({ ok: false });
  await expect(fetchExplore('overview', DEFAULT_SELECTION)).rejects.toThrow();
  fetch.mockResolvedValue({ ok: true, json: async () => { throw new Error('invalid JSON'); } });
  await expect(fetchExplore('overview', DEFAULT_SELECTION)).rejects.toThrow();
});
