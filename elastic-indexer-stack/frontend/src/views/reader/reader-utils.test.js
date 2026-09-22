import { citation, comparisonUrl, downloadText, highlightMarkdown, highlightParts, MAX_MARKS, readerUrl, recordId, searchPhrase, sourceLink, value } from './reader-utils';

test('reader URLs preserve opaque IDs and literal phrase searches', () => {
  const id = 'websites/example.org/20000101000000/a ?b=2#3%ü';
  const parsed = new URL(readerUrl(id, { compare: 'other?&id=oops', find: '"ancient cyclops"', part: 'ocr' }), 'https://search.eqarchives.org');
  expect(parsed.pathname).toBe('/document');
  expect(parsed.searchParams.get('id')).toBe(id);
  expect(parsed.searchParams.get('compare')).toBe('other?&id=oops');
  expect(parsed.searchParams.get('find')).toBe('"ancient cyclops"');
  expect(searchPhrase('"ancient cyclops"')).toBe('ancient cyclops');
  expect(searchPhrase('cyclops')).toBe('cyclops');
  expect(searchPhrase()).toBe('');
  expect(parsed.searchParams.get('part')).toBe('ocr');
  expect(readerUrl('x')).toBe('/document?id=x');
  expect(recordId({ _meta: { id: 'exact' }, id: { raw: 'wrong' } })).toBe('exact');
  expect(recordId({ id: { raw: 'legacy' } })).toBe('legacy');
  expect(recordId()).toBe('');
  expect(value({ a: { raw: 123 } }, 'a')).toBe('');
});

test('resolves safe relative links within the historical capture', () => {
  const base = 'https://web.archive.org/web/20000101000000id_/http://example.org/folder/page.html';
  expect(sourceLink('../spell.html?q=1', base)).toBe('https://web.archive.org/web/20000101000000id_/http://example.org/spell.html?q=1');
  expect(sourceLink('/root', base)).toBe('https://web.archive.org/web/20000101000000id_/http://example.org/root');
  expect(sourceLink('https://other.org/a', base)).toBe('https://other.org/a');
  expect(sourceLink('//other.org/a', base)).toBe('http://other.org/a');
  expect(sourceLink('part', 'https://example.org/a/')).toBe('https://example.org/a/part');
  for (const bad of ['', 'not a URL', 'javascript:alert(1)', 'data:text/html,hi', 'https://user:password@example.org']) expect(sourceLink(bad)).toBeUndefined();
});

test('citations label capture time without inventing a publication date or author', () => {
  const record = { title: { raw: 'Guide' }, capture_date: { raw: '2000-01-01' }, url: { raw: 'https://example.org/a' }, llm_guessed_date: { raw: '1999' } };
  expect(citation(record, 'https://reader')).toBe('Guide. Captured 2000-01-01 (archive timestamp). https://example.org/a. EQ Archives: https://reader');
  expect(citation({}, 'https://reader')).toBe('Untitled document. EQ Archives: https://reader');
});

test('highlights literal regex characters, case and Unicode without altering source', () => {
  const text = 'İ start [a+b] and [A+B] end';
  const parts = highlightParts(text, '[a+b]');
  expect(parts.filter(p => p.match).map(p => p.text)).toEqual(['[a+b]', '[A+B]']);
  expect(parts.map(p => p.text).join('')).toBe(text);
  expect(highlightParts('abc', '')).toEqual([{ text: 'abc', match: false }]);
  expect(highlightParts('abc', 'missing')).toEqual([{ text: 'abc', match: false }]);
  expect(highlightParts('X', 'x')).toEqual([{ text: 'X', match: true }]);
  const many = 'a '.repeat(MAX_MARKS + 10);
  const bounded = highlightParts(many, 'a');
  expect(bounded.filter(p => p.match)).toHaveLength(MAX_MARKS);
  expect(bounded.map(p => p.text).join('')).toBe(many);
});

test('Markdown highlights share a limit across nodes and leave links and markup intact', () => {
  const tree = { children: [{ type: 'element', tagName: 'a', properties: { href: 'https://example.org' }, children: [{ type: 'text', value: 'word '.repeat(MAX_MARKS) }] }, { type: 'text', value: 'word unchanged' }, { type: 'element', tagName: 'br' }] };
  highlightMarkdown({ term: 'word' })(tree);
  expect(tree.children[0].children.filter(n => n.tagName === 'mark')).toHaveLength(MAX_MARKS);
  expect(tree.children[0].properties.href).toBe('https://example.org');
  expect(tree.children[1]).toEqual({ type: 'text', value: 'word unchanged' });
});

test('downloads the exact text with a safe filename and releases the temporary URL', async () => {
  jest.useFakeTimers();
  URL.createObjectURL = jest.fn(() => 'blob:source');
  URL.revokeObjectURL = jest.fn();
  let filename;
  const click = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { filename = this.download; });
  try {
    downloadText('exact\r\ntext without final newline', 'a/b?c');
    expect(filename).toBe('a-b-c.txt');
    const blob = URL.createObjectURL.mock.calls[0][0];
    const contents = new Promise(resolve => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(blob); });
    jest.useRealTimers();
    expect(await contents).toBe('exact\r\ntext without final newline');
    jest.useFakeTimers();
    downloadText('second', '');
    expect(filename).toBe('archive-document.txt');
    jest.runOnlyPendingTimers();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:source');
    expect(document.querySelector('a[download]')).toBeNull();
  } finally { click.mockRestore(); jest.useRealTimers(); }
});


test('comparison order uses UTC timestamps and keeps a stable direction for unknown dates', () => {
  const selected = { _meta: { id: 'selected' }, capture_date: { raw: '2000-01-01T10:00:00' } };
  const earlier = { _meta: { id: 'earlier' }, capture_date: { raw: '2000-01-01T09:00:00Z' } };
  expect(comparisonUrl(selected, earlier)).toBe('/document?id=earlier&compare=selected');
  expect(comparisonUrl(selected, { _meta: { id: 'unknown' } })).toBe('/document?id=selected&compare=unknown');
});
