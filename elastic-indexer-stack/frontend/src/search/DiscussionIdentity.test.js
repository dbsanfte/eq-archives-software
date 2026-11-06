import { discussionIdentity, discussionMatchKey, excerptFind, excerptText, snippetHtml } from './DiscussionIdentity';
const record = extra => ({ _meta: { id: 'websites/forum.example.org/20061213163803/eq/board/print?message.id=20' },
  discussion: { raw: { version: 'forum-discussions-v1', key: 'a'.repeat(64), platform: 'lithium', origin: 'http://forum.example.org',
    forum: '/eq/Veterans', topic: '10', title: 'A discussion', source_sha256: 'b'.repeat(64), evidence_sha256: 'c'.repeat(64), ...extra } } });

test('requires source-bound verified metadata and a compatible exact website capture', () => {
  expect(discussionIdentity(record())).toMatchObject({ title: 'A discussion' });
  for (const fields of [{ version: 'unknown' }, { key: 'bad' }, { platform: 'unknown' }, { topic: 'zero' }, { forum: '' },
    { title: ' ' }, { origin: 'http://other.example.org' }, { source_sha256: '' }, { evidence_sha256: '' }]) {
    expect(discussionIdentity(record(fields))).toBeNull();
  }
  expect(discussionIdentity({})).toBeNull();
  expect(discussionIdentity({ ...record(), parent_id: { raw: 'parent' } })).toBeNull();
});

test('deduplicates identical highlighted excerpts only and preserves escaped text without executing it', () => {
  const item = { text_full: { snippet: 'A <em>match</em> &amp; &lt;script&gt;value&lt;/script&gt;' } };
  expect(excerptText(item)).toBe('A match & <script>value</script>');
  expect(discussionMatchKey(item)).toBe(discussionMatchKey({ text_full: { snippet: 'A match &amp; &lt;script&gt;value&lt;/script&gt;' } }));
  expect(discussionMatchKey(record())).toBe(record()._meta.id);
  expect(discussionMatchKey({ id: { raw: 'fallback' } })).toBe('fallback');
});


test('normalizes the connector’s real highlight-array shape before deduplicating excerpts', () => {
  const item = { text_full: { snippet: ['A <em>match</em> &amp; detail'] } };
  expect(excerptText(item)).toBe('A match & detail');
  expect(discussionMatchKey(item)).toBe(discussionMatchKey({ text_full: { snippet: 'A match &amp; detail' } }));
  expect(snippetHtml({ text_full: { snippet: [null, ''] } })).toBe('');
  expect(snippetHtml({ text_full: { snippet: 42 } })).toBe('');
});

test('reader links keep a literal text span across Markdown, table and encoded HTML formatting', () => {
  const result = snippet => ({ text_full: { snippet: [snippet] } });
  expect(excerptFind(result('[](/i/p.gif) I dont find <em>splitpaw</em> to be that <em>good</em> of exp.')))
    .toBe('I dont find splitpaw to be that good of exp.');
  expect(excerptFind(result('| Author |\n| --- | --- |\n| This is **a very long literal passage**. |'))).toBe('a very long literal passage');
  expect(excerptFind(result('body<br>Another longer source passage &amp;amp; end'))).toBe('Another longer source passage & end');
  expect(excerptFind(result('[A label](https://example.org/a-very-long-invisible-link-destination) Visible source passage.'))).toBe('Visible source passage.');
  expect(excerptFind(result('A lengthy unrelated source header in the same fragment.\nThe <em>revamp</em> was dangerous.'))).toBe('The revamp was dangerous.');
  const cached=result('A cached passage');
  expect(excerptFind(cached)).toBe(excerptFind(cached));
  expect(excerptFind(result('x'.repeat(300)))).toHaveLength(240);
  expect(excerptFind({})).toBe('');
});
