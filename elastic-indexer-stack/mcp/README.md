# EQ Archives MCP connector

Public endpoint: **https://search.eqarchives.org/mcp** (Streamable HTTP, anonymous,
read-only). The [connection guide](https://search.eqarchives.org/chatgpt.html)
explains custom ChatGPT connections. Public directory submission is a separate
step; [listing materials](../../docs/chatgpt-listing.md) are prepared for it.

## Contract

| Tool | Input | Output |
| --- | --- | --- |
| `search` | `query`: 1–1000 characters | `results`: up to ten `{id, title, url}` sources |
| `fetch` | `id`: exact search result ID, 1–2048 characters | `{id, title, text, url, metadata}` |

Both tools declare output schemas, read-only/non-destructive/idempotent annotations,
and anonymous authentication metadata. Results are returned in `structuredContent`
and as the same JSON in a text item, following OpenAI's
[research MCP contract](https://developers.openai.com/api/docs/mcp).
The service uses the official Python MCP SDK, stateless JSON responses and no
sticky sessions. GET/DELETE return 405: there are no unsolicited events or sessions.

Search combines source-text/title retrieval with existing 768-dimensional Nomic
vectors (source chunks, summaries and images). Explicit phrases and simple
operators use lexical search so semantic matches do not bypass constraints.
Natural-language searches can find related material, not only exact words.
Refine the query to investigate beyond the first ten results; this interface does
not expose arbitrary Elasticsearch DSL, index names or pagination.

`fetch` uses an exact Elasticsearch ID query. Archive paths remain IDs, never
filesystem paths or request URLs. Original extracted text is returned without
snippet truncation. Image transcriptions and estimated dates are clearly labelled;
generated summaries are not substituted for source text. Source URLs are preferred
for citations, with encoded search permalinks as a fallback. Very large upstream
responses over 8 MiB fail explicitly rather than returning a silently cut-off source.

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
HTTP initialization, tool discovery, schemas, invalid input, body limits, origin
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
against the real CPU Nomic service and an isolated Elasticsearch fixture. Master
publishes `dbsanfte/frontend:mcp-<git-sha>` alongside the frontend artifact in the
existing registry repository. Kustomize resolves their two digests independently.

The `eqarchives-mcp` Deployment/Service and exact `/mcp` HTTPS Ingress are managed
by [`mcp.yaml`](../k8s-manifests/mcp.yaml). One replica rolls with zero unavailable;
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
authenticated per-user quota. Review traffic/capacity before promoting the public
listing. The service does not log request bodies or intentionally store conversations.
Upstream/proxy operational logging is separate. Public descriptions must not promise
retention periods for infrastructure that has not been audited.

After deployment, run `python3 scripts/check-mcp.py https://search.eqarchives.org/mcp`
from the repository root. This validates discovery, search, full-text fetch and a
missing document against the real public archive. An official SDK client can also
connect with `mcp.client.streamable_http.streamable_http_client` and `ClientSession`.
Validate an actual ChatGPT conversation and Deep Research run from an eligible account
before directory submission; a protocol smoke test cannot certify account availability
or model behavior.
