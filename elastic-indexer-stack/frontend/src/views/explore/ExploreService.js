export const DEFAULT_SELECTION = Object.freeze({
  start: '1999-01-01', end: '2006-12-31', basis: 'capture_date', theme: '', site: '', phrase: ''
});

function validDate(value) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) &&
    new Date(value).toISOString().slice(0, 10) === value;
}

export function validSelection(value) {
  return validDate(value.start) && validDate(value.end) && value.start <= value.end &&
    value.start >= '1990-01-01' && value.end <= '2099-12-31' &&
    Number(value.end.slice(0, 4)) - Number(value.start.slice(0, 4)) <= 49 &&
    ['capture_date', 'llm_guessed_date'].includes(value.basis) &&
    [['theme', 80], ['site', 255], ['phrase', 100]].every(([key, max]) =>
      typeof value[key] === 'string' && value[key].length <= max && Array.from(value[key]).every(char => char.charCodeAt(0) >= 32));
}

export function readSelection(search) {
  const params = new URLSearchParams(search);
  const selection = { ...DEFAULT_SELECTION };
  for (const key of Object.keys(selection)) if (params.has(key)) selection[key] = params.get(key);
  const invalid = !validSelection(selection) || [...params.keys()].some(key =>
    !Object.hasOwn(selection, key) || params.getAll(key).length !== 1);
  return { selection: invalid ? { ...DEFAULT_SELECTION } : selection, invalid };
}

export function selectionParams(selection) {
  return new URLSearchParams(Object.entries(selection).filter(([, value]) => value !== '')).toString();
}

export function exploreSearchUrl(selection) {
  const params = new URLSearchParams();
  // A source phrase is a literal keyword constraint, so semantic search cannot
  // broaden it. The date/site/theme constraints travel in native Search UI state.
  if (selection.phrase) params.set('q', `text_full:"${selection.phrase.replace(/["\\]/g, ' ')}"`);
  const filters = [{ field: selection.basis, type: 'range', values: [{
    from: `${selection.start}T00:00:00.000Z`, to: `${selection.end}T23:59:59.999Z`,
    name: `${selection.start} – ${selection.end}`, isDateField: true
  }] }, { field: 'id', type: 'range', values: [{
    from: 'websites/', to: 'websites0', name: 'Archived websites'
  }] }];
  if (selection.theme) filters.push({ field: 'llm_tags', type: 'all', values: [selection.theme] });
  if (selection.site) filters.push({ field: 'domain_name', type: 'all', values: [selection.site] });
  filters.forEach((filter, i) => {
    params.set(`filters[${i}][field]`, filter.field);
    params.set(`filters[${i}][type]`, filter.type);
    if (filter.type === 'range') Object.entries(filter.values[0]).forEach(([key, value]) =>
      params.set(`filters[${i}][values][0][${key}]`, value));
    else params.set(`filters[${i}][values][0]`, filter.values[0]);
  });
  // The lexical ID range keeps the websites/ collection through native Search
  // UI state, including blank queries and semantic branches. Archive IDs are
  // paths; the upper sentinel is immediately after the directory prefix.
  return `/?${params}`;
}

const count = value => Number.isSafeInteger(value) && value >= 0;
export async function fetchExplore(kind, selection, signal) {
  const response = await fetch(`/api/explore/${kind}?${selectionParams(selection)}`, { signal });
  if (!response.ok) throw new Error('Explore request failed');
  const data = await response.json();
  if (!Number.isFinite(Date.parse(data.generated_at)) || !data.selection ||
      Object.keys(DEFAULT_SELECTION).some(key => data.selection[key] !== selection[key])) throw new Error('Invalid exploration response');
  if (kind === 'overview') {
    if (!['records', 'sites_count', 'tagged'].every(key => count(data[key])) ||
        !['themes', 'sites'].every(key => Array.isArray(data[key]) && data[key].every(b =>
          typeof b.key === 'string' && b.key && count(b.count) && typeof b.approximate === 'boolean')) ||
        !Array.isArray(data.timeline) || data.timeline.some(b => !count(b.records) || !count(b.sites) ||
          !Number.isInteger(b.year) || b.year < 1990 || b.year > 2099)) throw new Error('Invalid overview');
  } else if (!['sampled_pages', 'sampled_sites', 'examined_captures', 'duplicate_captures', 'clipped_pages', 'excerpt_chars', 'sample_limit']
    .every(key => count(data[key])) || !Array.isArray(data.phrases) || data.phrases.some(p =>
      typeof p.text !== 'string' || !p.text || p.text.length > 100 || !count(p.pages))) throw new Error('Invalid phrases');
  return data;
}
