import { discussionIdentity, discussionMatchKey, excerptText, snippetHtml } from './DiscussionIdentity';
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
