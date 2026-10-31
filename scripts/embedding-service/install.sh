#!/usr/bin/env bash
# Run as root from a software checkout on the GPU host, after provisioning the key.
set -euo pipefail
cd "$(dirname "$0")/../.."
test "$(id -u)" = 0
test -s /etc/eqarchives-embeddings/openai_api_key
install -d /etc/systemd/system/nvidia-persistenced.service.d /etc/systemd/system/nvidia-cdi-refresh.service.d
install -m 644 scripts/embedding-service/nvidia-persistenced.conf /etc/systemd/system/nvidia-persistenced.service.d/60-eqarchives.conf
install -m 644 scripts/embedding-service/nvidia-cdi-persistence.conf /etc/systemd/system/nvidia-cdi-refresh.service.d/60-eqarchives-persistence.conf
systemctl daemon-reload
systemctl start nvidia-persistenced.service
nvidia-smi -pm 1
docker info >/dev/null
systemctl enable --now docker nvidia-cdi-refresh.path
install -d -m 755 /opt/eqarchives-embeddings /var/lib/eqarchives-embeddings/models
install -d -m 700 -o 1000 -g 1000 /var/lib/eqarchives-embeddings/cache
install -m 644 scripts/embedding-service/{service.py,benchmark.py,cuda.env} /opt/eqarchives-embeddings/
install -m 644 elastic-indexer-stack/k8s-manifests/embeddings/runtime.env /opt/eqarchives-embeddings/
# Both files contain only version-controlled public configuration, never secrets.
source /opt/eqarchives-embeddings/runtime.env
source /opt/eqarchives-embeddings/cuda.env
model_path="/var/lib/eqarchives-embeddings/models/$MODEL_FILE"
if ! printf '%s  %s\n' "$MODEL_SHA256" "$model_path" | sha256sum --check --status; then
  curl --fail --location --retry 3 "$MODEL_URL" --output "$model_path.download"
  printf '%s  %s\n' "$MODEL_SHA256" "$model_path.download" | sha256sum --check --status
  mv "$model_path.download" "$model_path"
fi
chmod 644 "$model_path"
chown 1000:1000 /etc/eqarchives-embeddings/openai_api_key
chmod 600 /etc/eqarchives-embeddings/openai_api_key
docker pull "$CUDA_IMAGE"
install -m 644 scripts/embedding-service/eqarchives-embeddings.service scripts/embedding-service/eqarchives-embeddings-health.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now eqarchives-embeddings.service eqarchives-embeddings-health.timer
