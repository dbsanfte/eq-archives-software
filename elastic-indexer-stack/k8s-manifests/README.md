# Frontend deployment

`01-frontend.yaml` captures the production frontend on **eqvm**, which runs
single-node **k3s** directly (not k3d). It manages the existing four-replica
`search-eqarchives` Deployment, Service, Traefik routing and middleware, and
cert-manager Certificates in `eqarchives-es`.

The Kustomization deliberately includes only frontend resources. Elasticsearch,
its data, the embedding server, Traefik, and the `letsencrypt-prod` ClusterIssuer
are existing prerequisites. `00-elasticsearch.yaml` is a separate, manually
managed backend reference; its secret placeholders must never be applied by the
frontend workflow.

## GitHub Actions

[Frontend CI/CD](../../../.github/workflows/elastic-indexer-stack-cicd.yml) runs on
every push to `master`, every pull request targeting `master`, and manual dispatches. Pull
requests build and smoke-test on a GitHub-hosted runner, without production
credentials or access to the VM. Only `master` publishes and deploys.

The build uses the frozen Yarn lockfile and pinned base images, runs the
frontend tests with the configured 90% coverage thresholds, and smoke-tests the
final NGINX image. That same image is pushed
to Docker Hub as `dbsanfte/frontend:<git-sha>`. Deployment uses its immutable
`sha256` digest, not `latest`. Re-running a commit reuses and smoke-tests its
already published image instead of rebuilding or overwriting the tag.

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
secrets, applies the manifests, and waits for readiness. It then checks HTTPS and
an authenticated Elasticsearch search and archive count through the production
ingress.

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
| `FRONTEND_OPENAI_API_KEY` | Key passed to the existing embedding endpoint |

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
```

For a manual deployment on eqvm, export the five secret variables from a trusted
source and invoke the same script with a known image digest:

```sh
bash scripts/deploy-frontend.sh dbsanfte/frontend@sha256:FULL_IMAGE_DIGEST
```

Do not apply `01-frontend.yaml` directly: its image is a deliberate placeholder
that the script replaces. Do not apply the entire manifests directory.

A failed rollout makes the workflow fail. Readiness and `maxUnavailable: 0`
preserve healthy old pods during a failed container rollout. For immediate
recovery, `sudo kubectl -n eqarchives-es rollout undo deployment/search-eqarchives`
restores the preceding pod template (provided its runtime credentials still
work). Then revert the faulty change on `master` to make the recovery permanent.
Ingress and secret changes are not undone by `rollout undo`.

To confirm a repeat deployment is a no-op, compare the Deployment generation,
revision annotation, and pod UIDs before and after invoking the script twice
with the same image digest and secret values.
