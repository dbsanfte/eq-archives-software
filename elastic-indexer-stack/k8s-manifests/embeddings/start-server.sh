#!/bin/sh
set -eu

: "${MODEL_FILE:?Set MODEL_FILE}"
: "${MODEL_ALIAS:?Set MODEL_ALIAS}"
key_file=${API_KEY_FILE:-/run/secrets/openai_api_key}
test -s "$key_file"
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-/cache}

# Keep the benchmarked 512-token, single-slot settings. Hosted CI exercises the
# same server with LLAMA_DEVICE=none and LLAMA_GPU_LAYERS=0 on its CPU.
set -- /app/llama-server \
  --model "${MODEL_DIR:-/models}/$MODEL_FILE" --alias "$MODEL_ALIAS" \
  --host 0.0.0.0 --port 8080 --embedding --pooling mean \
  --ctx-size 512 --batch-size 512 --ubatch-size 512 --parallel 1 \
  --threads "${LLAMA_THREADS:-8}" --threads-batch "${LLAMA_THREADS:-8}" \
  --device "${LLAMA_DEVICE:-Vulkan0}" --gpu-layers "${LLAMA_GPU_LAYERS:-99}" \
  --flash-attn off --no-cache-prompt --cache-ram 0 \
  --api-key-file "$key_file" --no-webui --log-verbosity 4
if [ "${LLAMA_DEVICE:-Vulkan0}" = none ]; then
  set -- "$@" --no-op-offload
fi
exec "$@"
