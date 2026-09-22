#!/bin/sh
set -eu

: "${MODEL_FILE:?Set MODEL_FILE}"
: "${MODEL_URL:?Set MODEL_URL}"
: "${MODEL_SHA256:?Set MODEL_SHA256}"
model_dir=${MODEL_DIR:-/models}
model_path="$model_dir/$MODEL_FILE"
mkdir -p "$model_dir"

if [ -f "$model_path" ] && printf '%s  %s\n' "$MODEL_SHA256" "$model_path" | sha256sum --check --status; then
  echo 'Verified cached Nomic model.'
  exit 0
fi

# Publish only a complete, verified file; a failed download leaves the old inode
# intact for any existing server. Concurrent rollout pods use different files.
download=$(mktemp "$model_dir/.nomic-download.XXXXXX")
trap 'rm -f "$download"' EXIT HUP INT TERM
curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' \
  --connect-timeout 10 --max-time 120 --retry 3 --retry-all-errors --retry-delay 2 \
  --output "$download" "$MODEL_URL"
printf '%s  %s\n' "$MODEL_SHA256" "$download" | sha256sum --check --status
chmod 644 "$download"
mv "$download" "$model_path"
echo 'Downloaded and verified the pinned Nomic model.'
