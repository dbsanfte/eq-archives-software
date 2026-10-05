import { spawnSync } from 'child_process';
import path from 'path';

const bridge = path.resolve(__dirname, '../../scripts/search-benchmark.cjs');

function exportRequests(messages) {
  const result = spawnSync(process.execPath, [bridge], {
    env: { ...process.env, NODE_ENV: 'production', BROWSERSLIST_IGNORE_OLD_DATA: 'true' },
    input: messages.map(row => JSON.stringify(row)).join('\n') + '\n',
    encoding: 'utf8', timeout: 20000
  });
  expect(result.status).toBe(0);
  return result.stdout.trim().split('\n').map(line => {
    const reply = JSON.parse(line);
    expect(reply.error).toBeUndefined();
    return reply.result;
  });
}

test('exports the real lexical builder, constrained policy and semantic prefilters', () => {
  const vector = Array(768).fill(0.01);
  const filters = [{ field: 'mailing_list_name', values: ['eqbards'], type: 'all' }];
  const [plain, constrained, hybrid, weighted] = exportRequests([
    { op: 'build', query: { text: 'bard charm' } },
    { op: 'build', query: { text: 'charm AND bard NOT enchanter' }, vector },
    { op: 'build', query: { text: 'bard charm', filters }, vector },
    { op: 'build', query: { text: 'bard charm' }, fields: { title: 3, text_full: 1 } }
  ]);
  expect(plain.body.query.bool.should.map(q => q.multi_match.type))
    .toEqual(['best_fields', 'cross_fields', 'phrase', 'phrase_prefix']);
  expect(plain.body.query.bool.minimum_should_match).toBe(1);
  expect(plain.body._source).toEqual(expect.arrayContaining(['file_type', 'mime_type', 'llm_tags', 'llm_content_flavour']));
  expect(constrained.semantic_allowed).toBe(false);
  expect(constrained.body.knn).toBeUndefined();
  expect(constrained.body.query.bool.should[0].query_string.default_operator).toBe('AND');
  expect(hybrid.body.knn).toHaveLength(3);
  for (const branch of hybrid.body.knn) {
    expect(branch).toMatchObject({ k: 10, num_candidates: 100, boost: 5, query_vector: vector });
    expect(branch.filter).toContainEqual(hybrid.body.post_filter);
  }
  expect(weighted.body.query.bool.should[0].multi_match.fields).toEqual(['title^3', 'text_full^1']);
});

test('uses production grouping identity while preserving distinct sources and attachments', () => {
  const [keys] = exportRequests([{ op: 'keys', hits: [
    { _id: 'websites/example.org/20010101000000/Path?a=1', _source: {} },
    { _id: 'websites/example.org/20020101000000/Path?a=1', _source: {} },
    { _id: 'websites/example.org/20020101000000/path?a=1', _source: {} },
    { _id: 'websites/example.org/20020101000000/Path?a=2', _source: {} },
    { _id: 'other', _source: { url: 'https://web.archive.org/web/20020101000000/https://example.org/Path?a=1' } },
    { _id: 'mailing-lists/list/message.txt', _source: {} },
    { _id: 'websites/example.org/20020101000000/attachment', _source: { parent_id: 'parent' } }
  ] }]);
  expect(keys[0]).toBe(keys[1]);
  expect(new Set(keys).size).toBe(6);
  expect(keys[5]).toBe('record:mailing-lists/list/message.txt');
  expect(keys[6]).toBe('record:websites/example.org/20020101000000/attachment');
});

test('exports the date picker range contract instead of treating a range as a term', () => {
  const vector = Array(768).fill(0.01);
  const [request] = exportRequests([{ op: 'build', vector, query: {
    text: 'bard kiting', filters: [{ field: 'capture_date', type: 'range', values: [{
      from: '1999-01-01T00:00:00.000Z', to: '2002-12-31T23:59:59.999Z', isDateField: true, name: '01/01/1999 – 31/12/2002'
    }] }]
  } }]);
  const range = { range: { capture_date: { gte: '1999-01-01T00:00:00.000Z', lte: '2002-12-31T23:59:59.999Z' } } };
  expect(request.body.query.bool.filter).toEqual([{ bool: { filter: [range] } }]);
  for (const branch of request.body.knn) expect(branch.filter).toEqual(request.body.query.bool.filter);
});
