import { createConnector } from './Connector';
import ElasticsearchAPIConnector from '@elastic/search-ui-elasticsearch-connector';
import engine from '../config/engine.json';

jest.mock('@elastic/search-ui-elasticsearch-connector');
jest.mock('../config/config-helper', () => ({
  getConfig: () => jest.requireActual('../config/engine.json')
}));

beforeEach(() => jest.clearAllMocks());

test.each(['', 'ancient cyclops', '"ancient cyclops"'])('keeps cards lightweight without changing query/filter behavior: %s', searchTerm => {
  createConnector({ current: { enableSemanticSearch: false } });
  const postProcess = ElasticsearchAPIConnector.mock.calls[0][1];
  const request = postProcess({
    _source: { includes: [...engine.resultFields] },
    query: { match_all: {} }, sort: [{ _score: 'desc' }],
    highlight: { fields: { text_full: {}, llm_summary: {} } }
  }, { searchTerm });
  // Apply Elasticsearch's source selection to a representative large record.
  const source = { title: 'Ancient Cyclops', llm_summary: 'Summary',
    text_full: 'Large source text', text: [{ text_chunk: 'Chunk', vector: [1, 2] }],
    llm_image_text: 'Large OCR text', llm_summary_vector: [1, 2] };
  const excluded = name => (request._source.excludes || []).some(pattern =>
    new RegExp('^' + pattern.replace(/\./g, '\\.').replace(/\*/g, '.*') + '$').test(name));
  const selected = Object.fromEntries(Object.entries(source).filter(([name]) =>
    request._source.includes.includes(name) && !excluded(name)));
  expect(selected).toEqual({ title: 'Ancient Cyclops', llm_summary: 'Summary' });
  expect(request.highlight.fields.text_full).toEqual({
    fragment_size: 240, number_of_fragments: 1, no_match_size: 240
  });
  expect(request.highlight.encoder).toBe('html');
  expect(request.highlight.fields).not.toHaveProperty('text');
});
