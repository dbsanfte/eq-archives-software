#!/usr/bin/env bash
set -euo pipefail

# A published container port can reset/close connections before its server listens.
curl --fail --silent --show-error --retry 15 --retry-all-errors \
  --retry-delay 1 --max-time 5 "$@"
