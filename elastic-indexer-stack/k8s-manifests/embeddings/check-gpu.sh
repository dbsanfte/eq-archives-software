#!/bin/sh
set -eu

# Check the running server, not a second process that merely enumerates devices.
# Kernel DRM counters survive log rotation, so repeat deployments can verify a
# long-lived pod. The Q8_0 model and buffers occupy more than 100 MiB on this GPU.
for info in /proc/1/fdinfo/*; do
  driver=''
  vram=0
  gtt=0
  compute=0
  while read -r field value rest; do
    case "$field" in
      drm-driver:) driver=$value ;;
      drm-memory-vram:) vram=$value ;;
      drm-memory-gtt:) gtt=$value ;;
      drm-engine-compute:) compute=$value ;;
    esac
  done < "$info" 2>/dev/null || continue
  if [ "$driver" = amdgpu ] && [ "$((vram + gtt))" -ge 102400 ] && [ "$compute" -gt 0 ]; then
    echo 'Verified GPU model allocation and compute activity in the running Nomic server.'
    exit 0
  fi
done
echo 'The running Nomic server has no verified AMD GPU allocation/compute activity.' >&2
exit 1
