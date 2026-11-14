#!/usr/bin/env bash
# Run on eqvm after protected merge. Never restart the embedding/indexing services.
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
identity=${1:?Supply the private GPU-host SSH identity file}
known_hosts=${2:?Supply a verified SSH known_hosts file}
install -d -m 0700 -o dbsanfte -g dbsanfte /etc/eqarchives-reranker-tunnel
install -m 0600 -o dbsanfte -g dbsanfte "$identity" /etc/eqarchives-reranker-tunnel/identity
install -m 0600 -o dbsanfte -g dbsanfte "$known_hosts" /etc/eqarchives-reranker-tunnel/known_hosts
install -m 0644 "$source_dir/eqarchives-reranker-tunnel.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable eqarchives-reranker-tunnel.service
systemctl restart eqarchives-reranker-tunnel.service
