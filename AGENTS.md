# Automated development guide

These instructions apply throughout this repository. Follow the user's current
request, preserve unrelated work, and keep this guide consistent with the actual
workflows and deployment scripts when those change.

## Project and source of truth

This public repository, `dbsanfte/eq-archives-software`, contains the frontend and
indexing backend for https://search.eqarchives.org. The license is
**AGPL-3.0-only**; preserve the existing Elastic third-party notices. Archive
content lives in the separate `dbsanfte/eq-archives` repository.

All paths and commands below are relative to the repository root unless stated
otherwise. Consult the linked files for settings rather than copying stale image
digests, dependency versions, or credentials into new files.

| Subject | Authoritative files |
| --- | --- |
| Architecture, backend setup and environment variables | [README.md](README.md) |
| Frontend development, container configuration and browser tests | [Frontend guide](elastic-indexer-stack/frontend/README.md) |
| Public MCP contract, tests, limits and connection instructions | [MCP guide](elastic-indexer-stack/mcp/README.md), [public connection screen](elastic-indexer-stack/frontend/public/mcp.html) |
| Deployment, prerequisites, credentials and rollback | [Deployment guide](elastic-indexer-stack/k8s-manifests/README.md) |
| Required checks and production delivery | [Frontend CI/CD](.github/workflows/elastic-indexer-stack-cicd.yml) |
| Frontend build and coverage configuration | [Dockerfile](elastic-indexer-stack/frontend/Dockerfile), [package.json](elastic-indexer-stack/frontend/package.json), [Jest configuration](elastic-indexer-stack/frontend/jest.config.js) |
| Production reconciliation and runtime secrets | [deploy-frontend.sh](scripts/deploy-frontend.sh), [frontend-secrets.py](scripts/frontend-secrets.py) |
| Managed Kubernetes resources | [Root Kustomization](elastic-indexer-stack/k8s-manifests/kustomization.yaml), [frontend manifests](elastic-indexer-stack/k8s-manifests/01-frontend.yaml), [embedding manifests](elastic-indexer-stack/k8s-manifests/embeddings/deployment.yaml) |
| Model/image pins, bootstrap and GPU allocation | [runtime.env](elastic-indexer-stack/k8s-manifests/embeddings/runtime.env), [embedding Kustomization](elastic-indexer-stack/k8s-manifests/embeddings/kustomization.yaml), [device plugin](elastic-indexer-stack/k8s-manifests/embeddings/device-plugin.yaml) |
| Indexer dependencies, tests and mappings | [Indexer Dockerfile](elastic-indexer-stack/indexer/Dockerfile), [test requirements](elastic-indexer-stack/indexer/src/indexer/requirements-dev.txt), [tests](elastic-indexer-stack/indexer/src/indexer/test), [Elasticsearch manager](elastic-indexer-stack/indexer/src/indexer/es_manager.py) |

## Branching and delivery

1. Inspect `git status`, fetch `origin`, and start a descriptive branch from the
   current `origin/master`, such as `fix/mobile-filters` or `feat/search-options`.
   Do not overwrite another person's edits or include unrelated files.
2. Make the change and its regression tests together. Stage specific files,
   inspect the staged diff for secrets and unintended changes, and run
   `git diff --check` before committing.
3. Push the branch and create a pull request targeting `master`. Describe the
   resulting behavior and the checks actually performed. For multiline PR text,
   use a temporary file with `gh pr create --body-file`.
4. Keep the PR up to date with `master` and wait for the exact required check,
   **`Test and build frontend`**, to pass for the current revision before merging.
   Normal changes must use this protected PR path; never fabricate check results.
5. When deployment is part of the task, follow the subsequent `master` workflow
   through deployment and verify the live behavior before reporting completion.

`master` protection requires a PR, an up-to-date branch, and that GitHub Actions
check. It also applies to administrators. No additional reviewer approval is
required. Force pushes and branch deletion are disabled. Preserve these settings
for normal development; do not weaken them to get a failing change merged.

Use `[skip ci]` only when the user explicitly requests a documentation-only
exception. It skips push/PR workflows, including deployment, but does **not**
satisfy required checks or bypass branch protection. A skipped PR check remains
pending. Keep any explicitly authorized administrative exception limited to that
commit and restore the existing protections immediately afterward. A skipped
documentation commit does not change the live build SHA. See
[GitHub's skip behavior](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs).

Use the authenticated `gh` CLI for repository operations. The VM has an older
version; REST calls are a reliable fallback for unsupported flags:

```bash
gh api repos/dbsanfte/eq-archives-software/branches/master/protection
gh api 'repos/dbsanfte/eq-archives-software/actions/runs?branch=master&per_page=5' \
  --jq '.workflow_runs[] | {id,head_sha,status,conclusion,html_url}'
```

## Testing requirements

**Every bug fix needs a regression test.** Reproduce the failure before the fix
where practical, then verify that the same test passes afterward. Test observable
behavior rather than merely repeating implementation details. Include delayed
responses and rapid typing when changing asynchronous search behavior. For CSS,
layout, stacking and mobile interaction bugs, use a real browser; Jest's jsdom
environment cannot verify painting or element overlap.

### Frontend unit tests and build

Use **Node.js 22** and **Yarn Classic 1.22.22**. Keep `yarn.lock` in sync with
intentional dependency changes; use frozen installs for verification.

```bash
(
  cd elastic-indexer-stack/frontend
  yarn install --frozen-lockfile --non-interactive
  CI=true yarn test:ci --runInBand
  yarn build
)
```

Jest enforces at least **90% statements, branches, functions and lines**. Do not
lower thresholds or exclude changed behavior to make tests pass. Coverage reports
are generated under `elastic-indexer-stack/frontend/coverage/`. The Docker build
also runs the full Jest suite and coverage gate before compiling the application;
it can be used when Node/Yarn are unavailable on the host.

### Built image, browser and embedding checks

The following reproduces the main CI checks locally with Docker and Python 3:

```bash
docker build --build-arg GIT_SHA="$(git rev-parse HEAD)" \
  --tag eqarchives-frontend:local elastic-indexer-stack/frontend
python3 -m venv .venv
.venv/bin/pip install -r scripts/browser-test-requirements.txt
.venv/bin/playwright install --with-deps chromium
EXPECTED_GIT_SHA="$(git rev-parse HEAD)" \
  BROWSER_TEST_PYTHON="$PWD/.venv/bin/python" \
  bash scripts/smoke-frontend.sh eqarchives-frontend:local
bash scripts/smoke-embeddings.sh eqarchives-frontend:local
```

- [smoke-frontend.sh](scripts/smoke-frontend.sh) checks NGINX health, assets, build
  revision and credential isolation. Setting `BROWSER_TEST_PYTHON` also runs the
  [Chromium regressions](scripts/test-frontend-browser.py); CI always sets it.
  Without it, the script performs only the container smoke checks.
- Browser tests mock API responses and use disposable containers with dummy
  credentials. They cover sort-menu overlap with empty and floating date labels
  at 320, 390, 768 and 1280 px, plus sorting and date-picker interaction. Extend
  this suite for browser-dependent bugs. `PLAYWRIGHT_CHROMIUM_EXECUTABLE` can
  select an existing local Chromium binary; the default uses Playwright's install.
- [smoke-embeddings.sh](scripts/smoke-embeddings.sh) downloads the pinned model,
  verifies its hash and offline cache reuse, and runs the real server on CPU. It
  checks authentication, the NGINX proxy, 768-dimensional embeddings, oversized
  input rejection and recovery. No production credentials or GPU are needed.

For deployment-script or manifest changes, also run the workflow's validation:

```bash
bash -n scripts/deploy-frontend.sh scripts/smoke-frontend.sh scripts/smoke-embeddings.sh
sh -n elastic-indexer-stack/k8s-manifests/embeddings/download-model.sh
sh -n elastic-indexer-stack/k8s-manifests/embeddings/start-server.sh
sh -n elastic-indexer-stack/k8s-manifests/embeddings/check-gpu.sh
python3 -m py_compile scripts/frontend-secrets.py scripts/check-embeddings.py scripts/test-frontend-browser.py
kubectl kustomize elastic-indexer-stack/k8s-manifests >/dev/null
```

For documentation-only changes, check the diff, referenced paths, commands and
accuracy against the source files; application regression tests are unnecessary.
This does not itself exempt a normal PR from the required CI check.

### Indexing backend

Use Python **3.13**, Git and system `libmagic`, as specified in the indexer
Dockerfile. Backend changes need their own pytest verification; the required
frontend workflow does not run backend tests or deploy the indexer.

```bash
(
  cd elastic-indexer-stack/indexer
  python3.13 -m venv .venv
  .venv/bin/pip install -r src/indexer/requirements-dev.txt
  PYTHONPATH=src .venv/bin/python -m pytest src/indexer/test
)
```

The backend dependency set includes substantial document and ML tooling. Keep
test work isolated from live ingestion. Both worker modes require configured
Elasticsearch, RabbitMQ, model endpoints and a shared archive checkout; see the
root README for environment variables and secret mounts.

### Public MCP service

The service in `elastic-indexer-stack/mcp` uses Python 3.13 in production and
supports 3.10+ for local development. Install its hash-locked
`requirements-dev.txt` and run `python -m pytest` from that directory. Tests
include HTTP initialization, tool schemas, search/fetch behavior, source fidelity,
embedding fallback, cache behavior, credential isolation and request limits.
Branch coverage is enabled with a 90% combined coverage threshold; do not lower it.
The MCP Docker build runs these tests before creating the production runtime.

Pass an MCP image as the second argument to `scripts/smoke-embeddings.sh` to
exercise the real MCP container with CPU Nomic and the isolated Elasticsearch
fixture and the production ingress rules in an isolated Traefik container; CI always
does this. The routing test uses `scripts/deployment-test-requirements.txt`,
`DEPLOYMENT_TEST_PYTHON` and the pinned test image in `scripts/routing-test.env`.
Keep MCP's explicit ingress priority: the root `PathPrefix` rule is longer than the
exact MCP rule and otherwise wins Traefik's default ordering. After deployment run
`python3 scripts/check-mcp.py https://search.eqarchives.org/mcp`.

Preserve `search(query)` and `fetch(id)` compatibility, output schemas, read-only
annotations and matching structured/JSON text results. Document IDs are opaque
exact ES IDs, never filesystem paths or URLs. Do not silently truncate source
text or substitute generated summaries; keep OCR/date estimates labelled.
Never expose upstream credentials, errors, vectors or arbitrary Elasticsearch DSL.
Keep retrieval/cache limits appropriate for the shared single-slot model server.
The user chose direct public MCP access with a top-right MCP icon and a connection
screen for ChatGPT developer mode and Claude. Do not prepare or submit an official
OpenAI directory listing unless requested later. Only claim account-level
ChatGPT/Claude or Deep Research validation after actually performing it.

## Application constraints

- React uses Elastic Search UI and Material UI. Shared appearance is defined in
  [ArchiveTheme.css](elastic-indexer-stack/frontend/src/views/ArchiveTheme.css)
  and [theme.js](elastic-indexer-stack/frontend/src/theme.js); keep the archive
  palette and mobile interactions consistent.
- Search responses must not replace newer text the user is typing. Preserve the
  existing asynchronous input regression coverage when changing search controls.
- The status bar exposes the deployed Git SHA. The Docker `GIT_SHA` argument
  becomes `REACT_APP_GIT_SHA`; retain the display and bundle smoke check.
- [engine.json](elastic-indexer-stack/frontend/src/config/engine.json) and all
  `REACT_APP_*` values are public browser configuration. They must contain no
  upstream credentials. The development server alone does not provide API proxies.
- Browser APIs stay on the same origin: `/elasticsearch/eq-archive/_search`,
  `/elasticsearch/eq-archive/_count`, and `/openai/v1/embeddings`. NGINX adds the
  upstream authorization headers from runtime secret files.
- Nomic queries use the `search_query:` prefix, while the embedding cache is
  keyed by the original query. Preserve the configured model alias and
  **768-dimensional** vector compatibility with the indexed data. Changing the
  embedding model is not just an interchangeable frontend setting.

## CI/CD behavior

The active delivery workflow is
[elastic-indexer-stack-cicd.yml](.github/workflows/elastic-indexer-stack-cicd.yml).
It runs for PRs targeting `master`, pushes to `master`, and manual dispatches.
There is no release-please workflow or release/tag prerequisite.

The required build job runs on GitHub-hosted `ubuntu-24.04`. It validates scripts
and Kustomize, builds the frontend with Jest/coverage, runs container and browser
checks, builds/tests MCP with coverage, and exercises both containers with the real
Nomic service on CPU. PRs get no production secrets
or self-hosted execution. Keep this required check present on every normal PR;
path-based workflow skipping can leave required checks pending.

Only `master` publishes `dbsanfte/frontend:<full-git-sha>` and the MCP artifact
`dbsanfte/frontend:mcp-<full-git-sha>` to Docker Hub and deploys their independent
immutable image digests. A rerun reuses an already published image
for that SHA and repeats smoke checks. Base images, third-party actions and the
embedding runtime/model are pinned; update those pins deliberately.

Deployment uses the self-hosted runner `eqvm`, with labels
`self-hosted`, `Linux`, `X64`, `eqvm`. The runner is a trusted VM administrator;
do not route untrusted PR code onto it. It uses the local kubeconfig rather than
a cluster-admin credential stored in GitHub. Deployments are serialized in the
`eqarchives-frontend-production` concurrency group and are not canceled midway.
A queued run checks that its SHA still matches `master` before touching the cluster.

The deploy script validates rendered resources, reconciles secrets, brings up
and verifies Nomic/GPU service, then rolls the frontend and MCP service. It checks
HTTPS, search, document count, embeddings and a vector-only search through the
production proxy, followed by public MCP discovery/search/fetch checks.
Repeated deployment of the same image/configuration/secrets must leave Deployment
generations, revisions and pod UIDs unchanged. Do not add timestamp annotations
or routine `rollout restart` calls. Configuration hashes and credential checksums
trigger updates when their contents actually change.

## Production infrastructure

The VM **eqvm runs single-node k3s directly**. Earlier references to k3d do not
describe this deployment. The usual checkout on this VM is
`/home/dbsanfte/eq-archives-software`. Use the kubeconfig at
`/etc/rancher/k3s/k3s.yaml`; passwordless `sudo -n kubectl` is available. Docker is
available for builds and isolated checks. Production resources are in namespace
**`eqarchives-es`**.

| Component | Production configuration |
| --- | --- |
| Frontend | Deployment/Service `search-eqarchives`, four replicas, NGINX port 80, `maxUnavailable: 0`, `maxSurge: 1` |
| Public routing | Traefik Ingress `search-eqarchives-root`, HTTPS `search.eqarchives.org`; `search-beta.eqarchives.org` redirects to the primary site |
| TLS | cert-manager, existing `letsencrypt-prod` ClusterIssuer; certificate Secrets remain cluster-managed |
| Elasticsearch | Existing service `elasticsearch.eqarchives-es.svc.cluster.local:9200`, index `eq-archive`; managed separately from frontend CI |
| Embeddings | Deployment/Service `nomic-embeddings`, one replica, internal endpoint `http://nomic-embeddings.eqarchives-es.svc.cluster.local:8080`, zero-unavailable rolling updates |
| MCP connector | Deployment/Service `eqarchives-mcp`, one replica, port 8080, exact public `/mcp` HTTPS ingress, zero-unavailable rolling updates; uses the existing read-only ES/model credentials |
| Model cache | PVC `nomic-embedding-models`, 1 GiB on existing `local-path` storage; reproducible model cache, not archive storage |
| GPU access | DaemonSet `eqarchives-vulkan-device-plugin`, AMD Radeon Vulkan device `/dev/dri/renderD128`, supplemental render GID 109 |

Nomic Embed v1.5 **Q8_0** runs in llama.cpp under alias
`text-embedding-nomic-embed-text-v1.5@q8_0`, with mean pooling, normalized vectors,
512-token context/batches, one request slot and eight host threads. All 13 layers
are offloaded through Vulkan. The device plugin exposes two shared
`eqarchives.org/igpu` allocations so an old and replacement pod can coexist; these
are not separate physical GPUs. Neither inference nor the device plugin requires
privileged mode. The pinned runtime rejects oversized inputs with HTTP 500 but
remains usable afterward; capacity changes need explicit testing.

The root Kustomization manages frontend, MCP and embedding resources only.
[00-elasticsearch.yaml](elastic-indexer-stack/k8s-manifests/00-elasticsearch.yaml)
and [docker-compose.yml](elastic-indexer-stack/docker-compose.yml) are backend
references with environment-specific settings/placeholders, not a deployment
recipe for this CI workflow. Elasticsearch data, RabbitMQ, archive ingestion and
enrichment services have separate lifecycles. Do not apply the whole manifests
directory or scale/reinitialize those services as part of a frontend change.

## Secrets

Never commit secrets, kubeconfigs, runtime secret manifests, real `.env` files or
generated NGINX configuration. Avoid printing credentials in tool output, shell
tracing, process arguments, workflow logs, artifacts or PR text. Kubernetes Secret
data is only base64-encoded, not safe to publish. Use placeholders in examples.

| Repository Actions secret | Purpose |
| --- | --- |
| `DOCKERHUB_USERNAME` | Docker Hub account for the frontend image |
| `DOCKERHUB_TOKEN` | Registry push/pull token |
| `FRONTEND_ES_USERNAME` | Dedicated read-only Elasticsearch account |
| `FRONTEND_ES_PASSWORD` | Password for that account |
| `FRONTEND_OPENAI_API_KEY` | Shared NGINX/Nomic embedding API key |

The deployment pipes these into `search-eqarchives-secrets` and
`dockerhub-pull-secret` using server-side apply, avoiding secret-bearing files and
last-applied annotations. NGINX reads `es_readonly_username`,
`es_readonly_password` and `openai_api_key` under `/run/secrets`. Nomic receives only
the shared API key. The current frontend account is `frontend-cicd` with role
`frontend-search`, restricted to reading `eq-archive`.

Use `gh secret set NAME --repo dbsanfte/eq-archives-software` with hidden input or
trusted stdin. Coordinate credential changes between upstream services, Actions
secrets and runtime consumers so healthy replicas remain available. Old history
contains a revoked legacy read-only password; do not restore historical browser
credentials or reuse revoked values. The deployment guide records that rotation.

## Operations and completion

For read-only inspection on eqvm:

```bash
eqarchives_kubectl=(sudo -n kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml)
"${eqarchives_kubectl[@]}" get nodes
"${eqarchives_kubectl[@]}" -n eqarchives-es get deployments,daemonsets,pods,services,pvc,ingresses
"${eqarchives_kubectl[@]}" -n eqarchives-es rollout status deployment/search-eqarchives --timeout=300s
"${eqarchives_kubectl[@]}" -n eqarchives-es rollout status deployment/nomic-embeddings --timeout=900s
"${eqarchives_kubectl[@]}" -n eqarchives-es logs deployment/nomic-embeddings -c download-model --tail=40
curl --fail --silent --show-error https://search.eqarchives.org/healthz
```

To redeploy the current `master` through the normal pipeline, when requested:

```bash
gh workflow run elastic-indexer-stack-cicd.yml \
  --repo dbsanfte/eq-archives-software --ref master
```

For an authorized manual deployment, load the five secret environment variables
from a trusted source and run `scripts/deploy-frontend.sh` with two known immutable
`dbsanfte/frontend@sha256:...` images: frontend first, MCP second. Use this script instead of applying the raw
manifests: their `deploy-via-ci` images are intentional placeholders. Full manual
deployment and idempotence procedures are in the deployment guide.

An emergency `kubectl -n eqarchives-es rollout undo deployment/search-eqarchives`
(using the same sudo/kubeconfig as above) restores the preceding pod template,
provided its credentials still work. It does not undo secrets or ingress changes.
For Nomic, undo `deployment/nomic-embeddings` and coordinate any shared-key change;
retain the cache PVC and device plugin. Follow operational recovery with a tested
revert through `master` so desired state matches production.

Legacy manual workflows are not routine diagnostics:
[diagnose-eqvm.yml](.github/workflows/diagnose-eqvm.yml) powers off a VirtualBox VM
and reinstalls VirtualBox, and [reboot-eqvm.yml](.github/workflows/reboot-eqvm.yml)
reboots the host. [diagnose-eqarchives-vm.yml](.github/workflows/diagnose-eqarchives-vm.yml)
targets the older `eqarchives-vm` runner label. Do not dispatch these during normal
development or service checks.

After a deployment, verify the workflow result, ready replicas, the status-bar
build SHA, and the changed behavior in a real browser. Include mobile verification
for responsive UI work. Report what changed, tests actually run, the PR/commit,
deployment status and any remaining limitation. Documentation-only work with an
explicit skip-CI request should finish without launching a deployment.
