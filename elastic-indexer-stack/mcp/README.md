# EQ Archives MCP server

Public endpoint: **https://search.eqarchives.org/mcp** (Streamable HTTP, anonymous,
read-only). The [connection guide](https://search.eqarchives.org/mcp.html)
explains ChatGPT developer mode and custom connections in Claude. The top-right
MCP icon on the search site opens the guide. Users connect directly to this server;
there is no official OpenAI directory submission or publisher account dependency.

## Contract

| Tool | Input | Output |
| --- | --- | --- |
| `search` | `query`: 1–1000 characters | `results`: up to ten `{id, title, url}` sources |
| `fetch` | `id`: exact search result ID, 1–2048 characters | `{id, title, text, url, metadata}` |
| `search_archive` | `query`, `filters`, `offset`, `limit`, `sort` | Paged keyword results with provenance, bounded totals and `next_offset` |
| `list_sources` | `source_type`, `query`, `filters`, `after`, `limit` | Exact source values and matching document counts, with `next_after` |

All tools declare output schemas, read-only/non-destructive/idempotent annotations,
and anonymous authentication metadata. Results are returned in `structuredContent`
and as the same JSON in a text item, following OpenAI's
[research MCP contract](https://developers.openai.com/api/docs/mcp).
The service uses the official Python MCP SDK, stateless JSON responses and no
sticky sessions. GET/DELETE return 405: there are no unsolicited events or sessions.

Search combines source-text/title retrieval with existing 768-dimensional Nomic
vectors (source chunks, summaries and images). Explicit phrases and simple
operators use lexical search with AND as the default join so excluded terms cannot
broaden the search and semantic matches do not bypass constraints. Use `|` for
explicit alternatives.
Natural-language searches can find related material, not only exact words.
The original `search(query)` and `fetch(id)` input/output contracts remain unchanged.
Use `search_archive` to investigate beyond the first ten results.

`fetch` uses an exact Elasticsearch ID query. Archive paths remain IDs, never
filesystem paths or request URLs. Original extracted text is returned without
snippet truncation. Image transcriptions and estimated dates are clearly labelled;
generated summaries are not substituted for source text. Source URLs are preferred
for citations, with encoded search permalinks as a fallback. Very large upstream
responses over 8 MiB fail explicitly rather than returning a silently cut-off source.

## Filtered research and source discovery

`search_archive` uses keyword matching, including the same phrase/operator rules,
without semantic broadening or embedding requests. Empty `query` browses the
filtered archive. Filters are optional:

- `domains`, `mailing_lists`, `file_types`: up to ten exact values per category.
  Values within a category are OR; different categories are AND. Discover values
  with `list_sources` instead of guessing. Domain prefixes such as `www` matter.
- `date_field`: `capture_date` (default) or `estimated_publication_date`.
  The latter filters `llm_guessed_date`, a model estimate, not a verified date.
- `date_from`, `date_to`: inclusive UTC calendar days in `YYYY-MM-DD` form.
  Either bound can be omitted. Impossible or reversed dates are rejected.
  A date range excludes documents without that date.

`limit` defaults to 20 and allows 1–50 results; `offset` defaults to zero.
Pass `next_offset` with unchanged query/filters/sort/limit to continue. `sort`
accepts `relevance`, `capture_date_asc`, `capture_date_desc`,
`estimated_publication_date_asc` and `estimated_publication_date_desc`.
Unknown dates sort last. Archive IDs break ties; a fixed replica preference and
document-order fallback retain legacy records missing an ID field.
Pagination reads the live index, not a frozen snapshot: indexing changes can
shift pages. Deduplicate by returned ID when collecting a long-running study.

The shared public server exposes a **1,000-result window per search**, trimming
the last page to that boundary. `total.relation` is `eq` for an exact count and
`gte` for a lower bound (counting is bounded at 1,001). `has_more` can remain true
when `next_offset` is null: `limit_reached=true` explicitly asks the caller to
narrow the query or filters, rather than implying the archive is exhausted.
Result metadata includes source/date provenance; full text remains in `fetch`.

`list_sources` accepts `source_type` of `domain` (default), `mailing_list` or
`file_type`, plus the same optional keyword query and filters. It returns
alphabetically ordered values and matching document counts, omitting empty/missing
values. `limit` defaults to 20 and allows 1–100; pass `next_after` as `after` with
unchanged source type/query/filters to continue. The cursor comes from
Elasticsearch's composite aggregation, not the last displayed value. A final page
can be empty. Source discovery does not use embeddings or hold server sessions.

For example, discover mailing lists with
`list_sources(source_type="mailing_list", query="ancient cyclops")`, then call
`search_archive` with:

```json
{
  "query": "\"ancient cyclops\"",
  "filters": {
    "mailing_lists": ["eq_wizards"],
    "date_field": "estimated_publication_date",
    "date_from": "1999-01-01",
    "date_to": "2001-12-31"
  },
  "limit": 20,
  "offset": 0,
  "sort": "estimated_publication_date_asc"
}
```

Fetch selected IDs to check the original evidence and date estimates. Neither
tool accepts arbitrary Elasticsearch DSL, field names, index names or URLs.

## Local development and tests

Production uses Python 3.13. Local development also supports Python 3.10+.

```sh
cd elastic-indexer-stack/mcp
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pytest
```

Tests exercise retrieval, query constraints, unavailable/busy embeddings, cache
expiry/eviction, source fidelity, missing IDs, sanitized failures, credentials,
inclusive date/source filters, legacy records, paged results/source values,
window limits, HTTP initialization, tool discovery, schemas, invalid input, body limits, origin
validation and client lifetime across requests. Branch coverage is enabled and the
combined coverage gate is **90%**. The Docker build runs this same suite before
producing a non-root runtime without the test dependencies.

`requirements*.in` declare direct dependencies; `requirements*.txt` pin all
dependencies and hashes. To intentionally update the lockfiles, use pip-tools:

```sh
pip-compile --generate-hashes --strip-extras -o requirements.txt requirements.in
pip-compile --generate-hashes --strip-extras -c requirements.txt -o requirements-dev.txt requirements-dev.in
```

For a local server, mount a directory containing `es_readonly_username`,
`es_readonly_password` and `openai_api_key`, readable by UID/GID 10001. Set
`ELASTICSEARCH_URL`, `ELASTICSEARCH_INDEX`, `EMBEDDING_URL` and `EMBEDDING_MODEL`
as needed; the production defaults are in `archive.py`. `SECRET_DIR` overrides
`/run/secrets`. Never use production credentials in tests or commit this directory.

```sh
docker build -t eqarchives-mcp:local elastic-indexer-stack/mcp # from repo root
# With a configured local instance:
python3 scripts/check-mcp.py http://127.0.0.1:8080/mcp
```

## Delivery and operational limits

The existing required `Test and build frontend` check also builds/tests this service.
`scripts/smoke-embeddings.sh FRONTEND_IMAGE MCP_IMAGE` checks the actual MCP container
against the real CPU Nomic service and an isolated Elasticsearch fixture. It also
tests the actual ingress rules/priorities with the same pinned Traefik release as
eqvm. Install `scripts/deployment-test-requirements.txt` and set
`DEPLOYMENT_TEST_PYTHON` if needed for the routing test renderer. Master
publishes `dbsanfte/frontend:mcp-<git-sha>` alongside the frontend artifact in the
existing registry repository. Kustomize resolves their two digests independently.

The `eqarchives-mcp` Deployment/Service and exact `/mcp` HTTPS Ingress are managed
by [`mcp.yaml`](../k8s-manifests/mcp.yaml). Explicit route priority prevents the
frontend's longer `PathPrefix` rule from capturing `/mcp`. One replica rolls with zero unavailable;
the frontend's routing and four replicas remain separate. Secrets are projected
from the existing read-only account and shared model key. Credential checksums
trigger a rollout only when values change. Normal connector changes do not restart
Nomic or change ingestion/Elasticsearch data.

Limits: four active retrieval calls per process, one embedding call at a time,
3-second embedding timeout, 15-second failure cooldown, lexical fallback, a
128-entry/5-minute in-memory embedding cache, 8-second Elasticsearch search timeout,
12-second HTTP timeout and 16 KiB request bodies. Queries over 600 characters skip
embeddings but retain their entire lexical query. Traefik additionally limits MCP
traffic to five requests/second with a burst of twenty and eight in-flight requests.
The cache is process-local; a rolling update can briefly have two active instances.

These limits protect the shared single-slot embedding service; they are not an
authenticated per-user quota. Review traffic/capacity as public usage grows.
The service does not log request bodies or intentionally store conversations.
Upstream/proxy operational logging is separate. Public descriptions must not promise
retention periods for infrastructure that has not been audited.

After deployment, run `python3 scripts/check-mcp.py https://search.eqarchives.org/mcp`
from the repository root. This validates discovery, search, full-text fetch,
missing documents, consecutive research pages, source discovery/filtering and
inclusive capture dates against the real public archive. The smoke query must
match at least 14 records and two domains. An official SDK client can also
connect with `mcp.client.streamable_http.streamable_http_client` and `ClientSession`.
Actual ChatGPT/Claude account availability and model behavior must be checked from
an eligible account; a protocol smoke test does not certify these. The service is
published directly at its HTTPS endpoint rather than through a provider directory.
