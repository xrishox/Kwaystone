#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME="${1:-all}"
OUT_DIR="${OUT_DIR:-$ROOT/dist/appimage}"

if [[ "$RUNTIME" == all ]]; then
  "$0" cpu
  "$0" nvidia
  exit 0
fi
if [[ "$RUNTIME" != cpu && "$RUNTIME" != nvidia ]]; then
  echo "usage: packaging/appimage/build.sh [cpu|nvidia|all]" >&2
  exit 2
fi

if [[ "${WAYSTONE_SKIP_SOURCE_HYGIENE:-}" != 1 ]]; then
  "$ROOT/scripts/check-source-hygiene" --strict
fi

engine="${CONTAINER_ENGINE:-}"
if [[ -z "$engine" ]]; then
  if command -v docker >/dev/null 2>&1; then
    engine=docker
  elif command -v podman >/dev/null 2>&1; then
    engine=podman
  else
    echo "Docker with BuildKit or Podman is required for reproducible AppImage builds" >&2
    exit 1
  fi
fi

mkdir -p "$OUT_DIR"
stage="$(mktemp -d "$OUT_DIR/.build-$RUNTIME.XXXXXX")"
trap 'rm -rf "$stage"' EXIT

if [[ "$engine" == docker ]]; then
  docker buildx build \
    --platform linux/amd64 \
    --build-arg "RUNTIME=$RUNTIME" \
    --target artifact \
    --output "type=local,dest=$stage" \
    -f "$ROOT/packaging/appimage/Containerfile" \
    "$ROOT"
else
  podman build \
    --platform linux/amd64 \
    --build-arg "RUNTIME=$RUNTIME" \
    --target artifact \
    --output "$stage" \
    -f "$ROOT/packaging/appimage/Containerfile" \
    "$ROOT"
fi

find "$stage" -maxdepth 1 -type f -exec mv -f {} "$OUT_DIR/" \;
ls -lh "$OUT_DIR"/*AppImage "$OUT_DIR"/SHA256SUMS-* 2>/dev/null
