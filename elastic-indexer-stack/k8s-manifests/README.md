# Frontend, MCP and embedding deployment

`01-frontend.yaml` captures the production frontend on **eqvm**, which runs
single-node **k3s** directly (not k3d). It manages the existing four-replica
`search-eqarchives` Deployment, Service, Traefik routing and middleware, and
cert-manager Certificates in `eqarchives-es`.

The Kustomization also manages the cluster-local `nomic-embeddings` Deployment,
Service, model-cache PVC, configuration, and Vulkan device plugin. Elasticsearch,
its data, Traefik, cert-manager, the `local-path` StorageClass, and the
`letsencrypt-prod` ClusterIssuer are existing prerequisites.
`00-elasticsearch.yaml` is a separate, manually managed backend reference;
its secret placeholders must never be applied by this workflow.

The public read-only MCP server is Deployment/Service `eqarchives-mcp`, defined
in [`mcp.yaml`](mcp.yaml). Its exact `/mcp` HTTPS route takes precedence over the
frontend's `/` route through explicit router priority, using the existing certificate.
Traefik otherwise prioritizes rule-string length, so `PathPrefix(/)` can beat the
shorter exact-path rule. CI reproduces this with a pinned Traefik container and
the matches/priorities rendered from the real manifests. The service has separate rate and
concurrency limits, one non-root replica, a read-only filesystem, projected
credentials and zero-unavailable rolling updates. The [MCP guide](../mcp/README.md)
documents its search/fetch contract and runtime limits.

## Nomic embedding service

The service uses the official [llama.cpp Vulkan image](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md)
and [Nomic Embed v1.5 Q8_0 GGUF](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF).
Public image digests, the exact model revision and download URL, and its SHA-256
are in [`embeddings/runtime.env`](embeddings/runtime.env). No mutable image or
model tag is used by the deployment script. The init container verifies the
139 MiB model on every startup, reuses a valid cached copy, and downloads into a
temporary file before publishing it atomically. The 1 GiB `local-path` PVC stores
only a reproducible model cache, not archive data.

The model runs as UID 1000 with mean pooling, normalized 768-dimensional output,
512-token context/batch sizes, one server slot, and eight host threads. All
13 layers are offloaded through Vulkan. Requests use the existing alias
`text-embedding-nomic-embed-text-v1.5@q8_0`. The frontend supplies Nomic's
`search_query:` prefix. There is no new public ingress: NGINX forwards only the
existing embedding route to `nomic-embeddings.eqarchives-es.svc.cluster.local:8080`.
The model server reads the same API key from a restricted Secret projection;
it receives no Elasticsearch credentials. Health probes do not require a key.

The pinned [generic device plugin](https://github.com/squat/generic-device-plugin)
advertises `/dev/dri/renderD128` as two shared `eqarchives.org/igpu` allocations
on `eqvm`. This permits one replacement pod to start while the old pod remains
ready; it does not represent two physical GPUs or provide memory partitioning.
The plugin mounts only the render-device directory and kubelet's device-plugin
socket directory. Neither it nor llama.cpp uses privileged mode. The inference
pod belongs to eqvm's render group (GID 109). Other hosts need corresponding
node, render-device, group, and storage settings; no ROCm installation is needed
for this Vulkan configuration.

Model and script ConfigMap hashes trigger a rollout when their content changes;
the API-key checksum triggers one when the key changes. Normal frontend commits
leave the model pod and cached model unchanged. The model deployment uses
`maxUnavailable: 0`, readiness checks, and a short drain period before shutdown.
Inputs exceeding 512 tokens are rejected; this pinned llama.cpp build reports
HTTP 500 with an `input ... is too large to process` error. The service remains
usable for subsequent requests. Larger contexts or indexing batch workloads
need separate configuration and capacity testing.

## GitHub Actions

[Frontend CI/CD](../../../.github/workflows/elastic-indexer-stack-cicd.yml) runs on
every push to `master`, every pull request targeting `master`, and manual dispatches. Pull
requests build and smoke-test on a GitHub-hosted runner, without production
credentials or access to the VM. Only `master` publishes and deploys.

The build uses the frozen Yarn lockfile and pinned base images, runs the
frontend tests with the configured 90% coverage thresholds, and smoke-tests the
final NGINX image. It also downloads and verifies the pinned model, proves cache
reuse without network access, runs the real llama.cpp server on CPU, checks API
authentication and a valid embedding through NGINX, and verifies that an oversized
input is rejected without breaking subsequent requests. That same frontend image is pushed
to Docker Hub as `dbsanfte/frontend:<git-sha>`. Deployment uses its immutable
`sha256` digest, not `latest`. Re-running a commit reuses and smoke-tests its
already published image instead of rebuilding or overwriting the tag.

The same required job builds the MCP image, enforces its 90% coverage gate,
tests the protocol and runs the actual container against CPU Nomic and a fake
Elasticsearch service. Master publishes this second artifact under
`dbsanfte/frontend:mcp-<git-sha>` in the existing Docker Hub repository, avoiding
new registry credentials. The deploy script takes **both immutable image digests**;
the `eqarchives-mcp` placeholder is independently replaced with its artifact.
The final deployment check exercises public MCP discovery, search, fetch and a
missing-source error against the real archive. MCP uses the existing restricted
ES account and embedding key; its credential checksum triggers a rolling update.

The top archive status bar shows the deployed build's seven-character Git SHA;
hover over it for the full revision. CI passes the commit through the Docker
`GIT_SHA` build argument and verifies that the browser bundle contains it. Local
builds without that argument display `Build: development`.

`master` requires a pull request with an up-to-date branch and a successful
`Test and build frontend` check from GitHub Actions. That check enforces the
configured 90% minimum coverage for statements, branches, functions, and lines.
The rule applies to administrators as well; direct pushes, force pushes, and
branch deletion are blocked. No additional reviewer approval is required.
Keep this check enabled for every pull request: path filters could otherwise
leave required checks pending indefinitely.

Deployment runs on the existing self-hosted runner with labels
`self-hosted`, `Linux`, `X64`, `eqvm`. It requires Docker for the runner's existing
operations, Python 3, curl, gh, and passwordless `sudo -n kubectl` using
`/etc/rancher/k3s/k3s.yaml`. No kubeconfig or cluster administrator credential is
stored in GitHub. This runner is a trusted administrator of this VM; keep write
access to `master` restricted to trusted maintainers.

Runs share one deployment concurrency group. A queued run checks that its commit
is still the tip of `master` before applying anything. Deployment renders the
image digest, validates the resources against the API, reconciles runtime
secrets, then reconciles the device plugin and embedding service. It confirms
readiness, GPU allocation/compute counters in the running server, and an authenticated embedding before applying the
frontend resources. It then checks HTTPS, an authenticated Elasticsearch search,
archive count, public embeddings, and a vector-only search through the production
ingress. A failed model startup leaves frontend routing unchanged.

Reapplying the same digest, configuration, and secrets keeps the same pod
template, so it does not trigger a rollout. There is no `rollout restart` or
timestamp annotation. A runtime secret checksum triggers a rolling update when
credentials change. The rolling strategy keeps all four existing replicas
available until replacements pass readiness checks.

## Secrets

Set these repository Actions secrets with `gh secret set NAME --repo
dbsanfte/eq-archives-software`, entering the value at the hidden prompt or piping
it from a trusted secret store. Never put values in command arguments or git.

| GitHub Actions secret | Purpose |
| --- | --- |
| `DOCKERHUB_USERNAME` | Docker Hub account with access to `dbsanfte/frontend` |
| `DOCKERHUB_TOKEN` | Docker Hub token with image push/pull permission |
| `FRONTEND_ES_USERNAME` | Elasticsearch user with read access to `eq-archive` |
| `FRONTEND_ES_PASSWORD` | That user's password |
| `FRONTEND_OPENAI_API_KEY` | Shared key for NGINX and the cluster-local Nomic server |

The deployment reconciles `search-eqarchives-secrets` and
`dockerhub-pull-secret` from these values using a pipe and server-side apply.
Secret values are never written to deployment manifests, workflow artifacts, or
kubectl's last-applied annotation. Certificate private keys stay in Secrets
managed by cert-manager. NGINX reads the application secrets at startup;
Elasticsearch credentials must not be added to `src/config/engine.json` or any
other browser asset.

The frontend now uses a dedicated `frontend-cicd` Elasticsearch user with the
`frontend-search` role, limited to read access to `eq-archive`. Its password is
stored only in Actions secrets and the runtime Kubernetes Secret.

The repository previously contained the legacy `readonly` user's password in
the browser configuration. The exposed password was rotated on 2026-09-22 in
Elasticsearch, `elastic-readonly-password-secret`, and the saved VM manifests.
The old value is rejected; frontend service continued without pod restarts.
Old commits and previously published images still contain the revoked value.
This legacy account is separate from `frontend-cicd`. Do not reintroduce browser
credentials or restore old secret values when merging old branches.

## Operations

To deploy the current `master` again:

```sh
gh workflow run elastic-indexer-stack-cicd.yml \
  --repo dbsanfte/eq-archives-software --ref master
gh run list --repo dbsanfte/eq-archives-software \
  --workflow elastic-indexer-stack-cicd.yml
sudo kubectl -n eqarchives-es rollout status deployment/search-eqarchives
sudo kubectl -n eqarchives-es rollout status deployment/nomic-embeddings
sudo kubectl -n eqarchives-es rollout status deployment/eqarchives-mcp
sudo kubectl -n eqarchives-es logs deployment/nomic-embeddings -c download-model
```

For a manual deployment on eqvm, export the five secret variables from a trusted
source and invoke the same script with a known image digest:

```sh
bash scripts/deploy-frontend.sh \
  dbsanfte/frontend@sha256:FRONTEND_IMAGE_DIGEST \
  dbsanfte/frontend@sha256:MCP_IMAGE_DIGEST
python3 scripts/check-mcp.py https://search.eqarchives.org/mcp
```

Do not apply the manifests directly: their images are deliberate placeholders
that the script replaces with pinned digests. Do not apply the entire manifests directory.

A failed rollout makes the workflow fail. Readiness and `maxUnavailable: 0`
preserve healthy old pods during a failed container rollout. For immediate
recovery, `sudo kubectl -n eqarchives-es rollout undo deployment/search-eqarchives`
restores the preceding pod template (provided its runtime credentials still
work). Then revert the faulty change on `master` to make the recovery permanent.
Ingress and secret changes are not undone by `rollout undo`.

For an embedding-specific rollback, use
`sudo kubectl -n eqarchives-es rollout undo deployment/nomic-embeddings`, then
revert the corresponding configuration change on `master`. Leave the PVC and
device plugin in place. The preceding hashed ConfigMaps remain available for
the previous pod template; an API-key rollback must also coordinate the key
used by NGINX and the model server.

For an MCP-only rollback, use
`sudo kubectl -n eqarchives-es rollout undo deployment/eqarchives-mcp`, then
revert the change through a tested PR. It does not change frontend or model pods.

To confirm a repeat deployment is a no-op, compare the Deployment generation,
revision annotation, and pod UIDs for all three deployments before and after invoking
the script twice with the same two image digests and secret values.
