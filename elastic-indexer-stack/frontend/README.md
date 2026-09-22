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
and deploy the tested image to k3s. See the
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
