import * as helpers from './config-helper';
import config from './engine.json';

const originalConfig = JSON.parse(JSON.stringify(config));
const originalEnvironment = process.env.NODE_ENV;
function configure(values) {
  Object.keys(config).forEach(key => delete config[key]);
  Object.assign(config, values);
}

beforeEach(() => {
  process.env.NODE_ENV = 'production';
  configure({ indexName: 'archives' });
  delete window.appConfig;
});
afterEach(() => {
  configure(originalConfig);
  process.env.NODE_ENV = originalEnvironment;
  delete window.appConfig;
});

test('selects file configuration, embedded configuration, and empty defaults', () => {
  expect(helpers.getConfig().indexName).toBe('archives');
  configure({});
  expect(helpers.getConfig()).toEqual({});
  window.appConfig = {};
  expect(helpers.getConfig()).toEqual({});
  window.appConfig = { engineName: 'embedded', titleField: 'subject' };
  expect(helpers.getConfig()).toBe(window.appConfig);
  process.env.NODE_ENV = 'test';
  expect(helpers.getConfig()).toEqual({});
});

test('maps archive fields into Elasticsearch search, result and autocomplete configuration', () => {
  configure({
    indexName: 'archives', searchFields: ['title', 'body'],
    resultFields: ['body'], titleField: 'title', urlField: 'url',
    thumbnailField: 'thumbnail', querySuggestFields: ['title']
  });
  expect(helpers.buildSearchOptionsFromConfig()).toEqual({
    search_fields: { title: {}, body: {} },
    result_fields: Object.fromEntries(['body', 'title', 'url', 'thumbnail'].map(
      field => [field, { raw: {}, snippet: { size: 100, fallback: true } }]
    ))
  });
  expect(helpers.buildAutocompleteQueryConfig()).toEqual({
    suggestions: { types: { documents: { fields: ['title'] } } }
  });
  expect(helpers.getTitleField()).toBe('title');
  expect(helpers.getUrlField()).toBe('url');
  expect(helpers.getThumbnailField()).toBe('thumbnail');
  const result = { getSnippet: jest.fn().mockReturnValue('Highlighted title') };
  expect(helpers.getResultTitle(result)).toBe('Highlighted title');
  expect(result.getSnippet).toHaveBeenCalledWith('title');
});

test('supports legacy fields and empty configuration without inventing result fields', () => {
  configure({ indexName: 'archives', fields: ['body'] });
  expect(helpers.buildSearchOptionsFromConfig().search_fields).toEqual({ body: {} });
  expect(helpers.buildSearchOptionsFromConfig().result_fields.body.raw).toEqual({});
  configure({ indexName: 'archives' });
  expect(helpers.buildSearchOptionsFromConfig()).toEqual({
    search_fields: undefined, result_fields: undefined
  });
  expect(helpers.getTitleField()).toBe('title');
  for (const value of [undefined, '', [], 'title']) {
    config.querySuggestFields = value;
    expect(helpers.buildAutocompleteQueryConfig()).toEqual({});
  }
});

test('omits metadata and displayed identity fields from additional result details', () => {
  configure({ indexName: 'archives', titleField: 'Subject', urlField: 'URL', thumbnailField: 'Thumb' });
  expect(helpers.stripUnnecessaryResultFields({
    _meta: {}, id: {}, subject: {}, url: {}, thumb: {}, content: { raw: 'body' }
  })).toEqual({ content: { raw: 'body' } });
  configure({ indexName: 'archives' });
  expect(helpers.stripUnnecessaryResultFields({ content: {} })).toEqual({ content: {} });
});

test('builds value, date, recent-date, and nested-date facets plus sort choices', () => {
  configure({
    indexName: 'archives', valueFacets: ['domain'], recentFacets: ['last_indexed'],
    datePickerFacets: ['capture_date'], nestedDatePickerFacets: ['extracted_dates'],
    sortFields: ['capture_date', 'title']
  });
  expect(helpers.getStandardFacetFields()).toEqual(['domain', 'last_indexed']);
  expect(helpers.getDatePickerFacetFields()).toEqual(['capture_date']);
  expect(helpers.getNestedDatePickerFacetFields()).toEqual(['extracted_dates']);
  expect(helpers.buildDatePickerFacetConfigFromConfig()).toEqual({
    capture_date: { type: 'range', ranges: [] }
  });
  const facets = helpers.buildStandardFacetConfigFromConfig();
  expect(facets.domain).toEqual({ type: 'value', size: 100 });
  expect(facets.last_indexed.ranges).toHaveLength(5);
  for (const range of facets.last_indexed.ranges) {
    expect(Date.parse(range.from)).toBeLessThan(Date.parse(range.to));
  }
  expect(helpers.getSortFields()).toEqual(['capture_date', 'title']);
  expect(helpers.buildSortOptionsFromConfig()).toEqual([
    { name: 'Relevance', value: '', direction: '' },
    { name: 'Captured date (Ascending)', value: 'capture_date', direction: 'asc' },
    { name: 'Captured date (Descending)', value: 'capture_date', direction: 'desc' },
    { name: 'Title (Ascending)', value: 'title', direction: 'asc' },
    { name: 'Title (Descending)', value: 'title', direction: 'desc' }
  ]);
  configure({ indexName: 'archives' });
  expect(helpers.getStandardFacetFields()).toEqual([]);
  expect(helpers.getSortFields()).toEqual([]);
  expect(helpers.buildDatePickerFacetConfigFromConfig()).toEqual({});
  expect(helpers.buildStandardFacetConfigFromConfig()).toEqual({});
  expect(helpers.buildSortOptionsFromConfig()).toHaveLength(1);
});
