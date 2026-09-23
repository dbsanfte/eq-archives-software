# EQ Archives Search frontend

The React application served at [search.eqarchives.org](https://search.eqarchives.org),
built with Elastic Search UI and Material UI. See the [project README](../../README.md)
for the architecture, indexing backend, and contribution guidance.

## Development and tests

Use Node.js 22 and Yarn Classic 1.22.22. From this directory:

```bash
yarn install --frozen-lockfile
yarn start
```

The development server listens at `http://localhost:3000`. It serves the UI, but
it does not provide the API proxy. For working search, use the NGINX container
below or a local reverse proxy that sends `/elasticsearch/` and `/openai/` to
that container while forwarding other requests to the development server.
Keep upstream credentials in NGINX, outside the browser.

```bash
yarn test:ci --runInBand
yarn test:watch
```

Tests use Jest and React Testing Library. Coverage must reach 90% for statements,
branches, functions, and lines. `coverage/lcov.info` contains the coverage report.

CI also runs Chromium regression tests against the built NGINX image. These
check that the sort menu covers both date labels at phone, tablet, and desktop
widths, including floating labels after dates are entered. Search responses are
mocked in the browser, so these tests need no running backend or real credentials.
To run them locally, from the repository root after building the image below:

```bash
python3 -m venv .venv
.venv/bin/pip install -r scripts/browser-test-requirements.txt
.venv/bin/playwright install --with-deps chromium
BROWSER_TEST_PYTHON="$PWD/.venv/bin/python" \
  bash scripts/smoke-frontend.sh eqarchives-frontend:local
```

## Date filters

**Captured date** and **Estimated content date** ranges stay applied across searches,
result updates, sorting and pagination. Choose both dates and click **Apply**
(or press Enter); **Clear** removes that range. Both calendar days are included
in UTC, including the end of the selected **To date**. Invalid or reversed
ranges cannot be applied. Estimated dates remain model-generated estimates.

Applied ranges are saved in the search URL, so bookmarks, reloads and browser
back/forward restore them. Unapplied edits remain drafts while results load;
reloading restores the applied range. Clearing the search text keeps filters.

Search UI owns the applied filter state. The date controls replace ranges with
`setFilter`, and new searches use `shouldClearFilters: false`. Query construction
preserves those filters for quoted/operator queries and all semantic kNN branches;
keyword matching remains required when a date filter is present. Jest and
Chromium regressions cover delayed results, successive searches, both date fields,
mobile filter toggling, URL history, complete-day bounds and timezone restoration.

## Grouped captures

Search results group repeated website captures by original page by default. The
first matching capture in the selected relevance/date order represents the group.
**View captures** opens the dated versions, newest first, with archive links and
full-text previews of each exact record. This history includes versions outside
the current query and filters. Disable **Group repeated captures** to return to
individual results. The toggle applies to the current visit; grouping is the
initial default.

Identity comes from the original URL inside a timestamped Wayback URL, with the
archive's `websites/<host>/<14-digit timestamp>/<path>` ID as a fallback. Hostname
case and fragments are normalized; path case, protocol, query strings, trailing
slashes, and `index.html` remain distinct. Messages, child attachments and records
with unknown identity remain separate. Titles alone never identify a group.

`GroupedSearch.js` scans lightweight results in batches of 50, caching one active
search and retaining unique groups across pages. It fills the requested page plus
a lookahead group, up to the existing 1,000-capture Elasticsearch browsing window.
This requires no index migration or vector rewrite. Previous/Next navigation and
capture counts avoid claiming an exact total of unique pages. Counts and facets
still describe matching **captures**. Changing the query, filters, date filters,
sort or semantic parameters invalidates the cache. Results come from the live
index; this is not snapshot pagination.

Capture history also pages in batches of 50, bounded at 1,000 candidate records.
It requests only metadata; previews load full text on demand. Exact ID regexes
escape Lucene operators. Older records without source IDs use a URL phrase lookup
followed by exact original-page validation, so similar URLs cannot enter the
history. Partial/error responses are rejected, collapsed histories cancel pending
requests, and late responses cannot replace current content. Both browsing limits
are labelled in the UI when reached.

Jest covers identities, cross-page grouping, limits, filters, asynchronous races,
history pagination/retry/cancellation and exact previews. Chromium covers grouped
pagination, independent query-string URLs, the all-captures toggle and selecting a
dated preview at phone and desktop widths. MCP tools retain their existing
individual-record behavior.

## Document reader and comparisons

Search cards show the short highlighted source excerpt before any generated
summary. They use a compact view with each **AI-generated summary** behind
an expandable disclosure. Result cards still fetch only lightweight metadata
and excerpts, not full document text.

**Read document** is the full-text action on search result cards. It opens a
dedicated reader with the complete indexed text,
formatted Markdown or exact source text, literal find/highlight navigation,
source links, capture provenance, citations and text downloads. The compact
archive header retains the deployed build SHA and MCP guide. OCR is a separate,
labelled model-generated image transcription; estimated publication dates are
also labelled. Neither reader nor comparison substitutes generated summaries for
missing source text. Remote images become links rather than loading automatically.
Relative links in Wayback captures resolve against the original page and retain
the selected archive timestamp.

Stable reader links use `/document?id=<opaque Elasticsearch ID>`. Optional
`find=<literal phrase>` highlights a passage; `part=ocr` selects transcription.
Result links carry the completed search term, so a newer unsubmitted input draft
cannot change which query the displayed results represent. **Copy Permalink**
copies a shareable reader link without the return path; older filtered-search
links continue to work.
Navigation from search results also carries a validated same-origin search URL
in an optional `return` parameter. **Back to results** restores that exact query,
filters, sort and page, including after a reader reload or capture comparison.
Direct reader links without a valid return URL lead to the archive search page.

**View captures → Compare** compares the selected capture with another version
of the same exact original page. A comparison URL adds `compare=<second ID>`;
**From** and **To** show the direction, and **Swap captures** reverses it. Unified
line differences label additions/removals in text as well as color, support change
navigation, and expand long unchanged sections. Each version remains available
through its reader link and complete download. Missing text is explicitly
unavailable, never treated as proof of an addition or deletion.

The reader bypasses the search provider and fetches exact IDs using size-one
queries through the existing same-origin proxy. `DocumentService.js` whitelists
source/OCR and provenance fields; vectors, nested chunks and summaries are not
requested. Aborted or superseded fetches/comparisons cannot replace current
content. No server routes, index migration or new service is required: NGINX's
existing SPA fallback serves direct reader/comparison links.

For responsive browsing, documents over 500,000 characters use complete plain
source text, and highlighting marks the first 1,000 matches. Interactive diffs
are bounded to 1,000,000 combined characters, 20,000 combined lines, a 1-second
algorithm deadline and 2,000 edits. Limits show an explicit fallback to full
readers/downloads without silently truncating text or claiming identical content.
The line comparison preserves whitespace and line endings. It compares indexed
text, so differences may reflect extraction/navigation as well as page edits.

Jest covers exact IDs, provenance, missing/partial/error/retry states, clipboard
fallback, literal/Unicode highlighting, full-text downloads, source link safety,
OCR, comparison limits and stale responses. Chromium covers entry from search,
query highlights, source mode, downloads/citations, capture selection, directional
diffs, swapping, direct reloads and mobile overflow at 320, 390 and 1280 px.

## Browser configuration

[`src/config/engine.json`](src/config/engine.json) controls the searchable fields
and presentation. It is bundled into the application, so changes require a
rebuild and every value is visible to site visitors.

| Setting | Purpose |
| --- | --- |
| `indexName` | Elasticsearch index or alias; must match the NGINX `ELASTICSEARCH_INDEX` |
| `searchFields`, `resultFields` | Fields to query and return |
| `querySuggestFields` | Fields used by the suggestion configuration |
| `titleField`, `urlField`, `thumbnailField` | Fields used to display results |
| `sortFields` | Available sort fields |
| `valueFacets`, `recentFacets` | Value and recent-indexing filters |
| `datePickerFacets`, `nestedDatePickerFacets` | Date-range filters |
| `vectorFields`, `nestedVectorFields` | Vector fields for semantic search |
| `embeddingModel` | Model used to embed search queries; must match the indexed vectors |

For Nomic Embed v1.5, the embedding client adds `search_query:` to the request
text while keeping its cache keyed by the original search. The production
deployment provides a local Q8_0 model under the existing
`text-embedding-nomic-embed-text-v1.5@q8_0` alias, with a 512-token input limit.

Do not put Elasticsearch passwords or model API keys in this file or in
`REACT_APP_*` variables. NGINX reads those credentials from runtime secret mounts.

## Build and run the container

The [Dockerfile](Dockerfile) installs the locked dependencies, runs the tests,
builds the React assets, and packages them with NGINX. To build from the repository
root:

```bash
cd elastic-indexer-stack
docker build \
  --build-arg GIT_SHA="$(git rev-parse HEAD)" \
  --tag eqarchives-frontend:local \
  ./frontend
```

Create an ignored `.env.frontend.local` file in `elastic-indexer-stack` with your
upstream addresses, for example:

```dotenv
ELASTICSEARCH_URL=http://elasticsearch.example.test:9200
ELASTICSEARCH_INDEX=eq-archive
OPENAI_URL=http://models.example.test:8000
```

Replace the example hosts with services reachable from the container.
`ELASTICSEARCH_URL` includes the port; `OPENAI_URL` is the model server URL
**without** `/v1`. The proxy appends `/v1/embeddings` itself. A container's
`localhost` refers to that container, not its host machine.

Using your editor or secret store, create these files in the same directory.
Each file should contain only the corresponding value:

| Local file | Container secret | Purpose |
| --- | --- | --- |
| `elastic.readonly.username.secret.txt` | `/run/secrets/es_readonly_username` | Elasticsearch user with read access to the search index |
| `elastic.readonly.password.secret.txt` | `/run/secrets/es_readonly_password` | That user's password |
| `openai_api_key.secret.txt` | `/run/secrets/openai_api_key` | Model endpoint API key |

All three files must be nonempty. The repository ignores `*.secret.txt` and local
`.env.*` files. Restrict their permissions and start the container:

```bash
chmod 600 elastic.readonly.username.secret.txt \
  elastic.readonly.password.secret.txt openai_api_key.secret.txt .env.frontend.local

docker run --rm --name eqarchives-frontend \
  --publish 127.0.0.1:3030:80 \
  --env-file .env.frontend.local \
  --mount "type=bind,src=$(pwd)/elastic.readonly.username.secret.txt,dst=/run/secrets/es_readonly_username,readonly" \
  --mount "type=bind,src=$(pwd)/elastic.readonly.password.secret.txt,dst=/run/secrets/es_readonly_password,readonly" \
  --mount "type=bind,src=$(pwd)/openai_api_key.secret.txt,dst=/run/secrets/openai_api_key,readonly" \
  eqarchives-frontend:local
```

Open `http://localhost:3030`. `GET /healthz` checks that NGINX is serving; a search
also verifies the Elasticsearch connection. The container does not provision
Elasticsearch, indexed data, or model servers.

## Static builds and revision display

From the `frontend` directory:

```bash
REACT_APP_GIT_SHA="$(git rev-parse HEAD)" yarn build
```

The output is written to `build/`. A deployment must also serve the NGINX proxy
routes in [nginx.conf.template](nginx.conf.template), or equivalent routes with
server-side authentication. Serving the static files alone does not provide
search access.

The top status bar displays the first seven characters of the build revision,
with the full SHA in its tooltip. Builds without a revision show
`Build: development`. The container build accepts `GIT_SHA`; GitHub Actions sets
it to the commit being deployed.

## Production deployment

Pull requests are tested and built automatically. Commits on `master` publish
and deploy the tested image and a Vulkan-backed Nomic embedding service to k3s. See the
[deployment guide](../k8s-manifests/README.md) for secrets, manifests, health
checks, idempotence, and rollback.

## License

The EQ Archives frontend is distributed under **AGPL-3.0-only**. See
[LICENSE.txt](LICENSE.txt), which contains the same license as the
[project license](../../LICENSE).

The application includes code adapted from Elastic's App Search Reference UI.
Its original Apache-2.0 terms and attribution are retained in
[NOTICE.txt](NOTICE.txt) and
[licenses/Elastic-Apache-2.0.txt](licenses/Elastic-Apache-2.0.txt).

## Search performance

Result cards download metadata and one short highlighted text excerpt. Raw full
text, OCR bodies, nested text chunks and embedding vectors stay out of result
responses. Nested vector matching remains enabled without returning unused
`inner_hits`. **Read document** loads the chosen document and its provenance by
exact Elasticsearch ID through the existing search proxy. The main result actions
link to the reader instead of mounting a preview dialog. The **Preview** action within
capture history uses a lightweight text-only request with loading, empty/error,
retry and cancellation states.

Embedding preparation and query construction share their eligibility rules in
`src/search/QueryPolicy.js`. Quoted/operator searches, disabled semantic search,
empty input, absent vector fields and an active service cooldown skip embedding
requests. New input and navigation abort obsolete requests. A five-second deadline
covers both headers and the body; failures fall back to keyword search. Completed
embeddings are retained in a bounded 50-entry browser cache. This cache is local
to the browser; MCP retains its own bounded cache.

NGINX enables gzip for JavaScript, CSS, JSON, text and SVG. Content-hashed files
under `/static/` use `Cache-Control: public, max-age=31536000, immutable`; HTML,
connection guides and unversioned assets use `no-cache` so they revalidate after a
deployment. Missing static files return 404 rather than the SPA shell and do not
receive an immutable cache policy. There is no shared API response cache.

`scripts/smoke-frontend.sh` runs the real NGINX compression/cache regression.
Browser tests verify compact result requests, direct reader navigation, capture
history previews, and semantic
search and keyword-only behavior at mobile and desktop widths. Jest covers
preview retries/cancellation, obsolete input, embedding deadlines and cache bounds.
