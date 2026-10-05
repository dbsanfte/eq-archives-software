/* Export actual frontend requests and capture identities without making HTTP calls.
 * Run with the frontend's frozen dependencies and Node 22 (see README.md).
 */
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const babel = require('@babel/core');
const frontend = path.resolve(__dirname, '..');
const originalLoader = require.extensions['.js'];
require.extensions['.js'] = (module, filename) => {
  if (!filename.startsWith(path.join(frontend, 'src') + path.sep)) return originalLoader(module, filename);
  const code = babel.transformSync(fs.readFileSync(filename, 'utf8'), {
    filename, babelrc: false, configFile: false,
    presets: [require.resolve('@babel/preset-react')],
    plugins: [require.resolve('@babel/plugin-transform-modules-commonjs')]
  }).code;
  module._compile(code, filename);
};
// Production modules log debugging information; stdout is a JSONL protocol.
console.log = () => {};
const dom = new (require('jsdom').JSDOM)('', { url: 'http://unused.invalid' });
global.window = dom.window;
global.document = dom.window.document;
const { createConnector } = require(path.join(frontend, 'src/search/Connector.js'));
const { createConfig } = require(path.join(frontend, 'src/config/Config.js'));
const { captureKey } = require(path.join(frontend, 'src/search/CaptureIdentity.js'));
const { usesQuerySyntax } = require(path.join(frontend, 'src/search/QueryPolicy.js'));
const { DEFAULT_KNN_PARAMS } = require(path.join(frontend, 'src/views/search/AdvancedSettings.js'));
const { embeddingService } = require(path.join(frontend, 'src/search/EmbeddingService.js'));
const engine = require(path.join(frontend, 'src/config/engine.json'));
const Connector = require('@elastic/search-ui-elasticsearch-connector').default;

async function handle(input) {
  if (input.op === 'contract') return { engine, defaults: DEFAULT_KNN_PARAMS };
  if (input.op === 'keys') return input.hits.map(hit => {
    const source = hit._source || {};
    const result = { _meta: { id: hit._id } };
    for (const [field, value] of Object.entries(source)) result[field] = { raw: value };
    return captureKey(result);
  });
  if (input.op !== 'build') throw new Error('Unknown bridge operation');
  const query = input.query;
  const state = { searchTerm: query.text, current: 1, resultsPerPage: 50,
    filters: query.filters || [], sortField: '', sortDirection: '', sortList: [] };
  const params = { current: { ...DEFAULT_KNN_PARAMS, ...input.params,
    enableSemanticSearch: Boolean(input.vector), groupCaptures: false } };
  embeddingService.cache.clear();
  if (input.vector) embeddingService.cache.set(query.text, input.vector);
  // Capture the real connector's postprocessor, including sorting and constraints.
  // The constructor assigns this hook as a public instance property.
  const connector = createConnector(params);
  const config = createConfig(connector).searchQuery;
  if (input.fields) config.search_fields = Object.fromEntries(
    Object.entries(input.fields).filter(([, weight]) => weight > 0).map(([field, weight]) => [field, { weight }])
  );
  let captured;
  const capture = new Error('Captured request');
  const original = connector;
  // A second connector uses the same production request postprocessing, exported
  // on the connector instance by Search UI. No transport is ever reached.
  const postProcess = original.postProcessRequestBodyFn;
  if (typeof postProcess !== 'function') throw new Error('Connector contract changed');
  const isolated = new Connector({ host: 'http://unused.invalid', index: engine.indexName }, (body, requestState) => {
    captured = postProcess(body, requestState);
    throw capture;
  });
  try { await isolated.onSearch(state, config); } catch (error) {
    if (!captured) throw error;
  }
  if (!captured) throw new Error('Request was not captured');
  // Avoid exporting large result/facet plumbing: ranking, filters and sort stay.
  delete captured.aggs;
  delete captured.highlight;
  captured._source = ['id', 'title', 'url', 'parent_id', 'domain_name', 'mailing_list_name', 'capture_date', 'llm_guessed_date',
    'file_type', 'mime_type', 'llm_tags', 'llm_content_flavour'];
  return { body: captured, semantic_allowed: !usesQuerySyntax(query.text) && Boolean(query.text.trim()) };
}

const lines = readline.createInterface({ input: process.stdin });
(async () => {
  for await (const line of lines) {
    try { process.stdout.write(JSON.stringify({ result: await handle(JSON.parse(line)) }) + '\n'); }
    catch (_) { process.stdout.write(JSON.stringify({ error: 'Frontend bridge failed; check frozen dependencies and frontend contract' }) + '\n'); }
  }
})();
