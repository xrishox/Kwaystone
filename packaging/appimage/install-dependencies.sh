#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME="${1:?cpu or nvidia runtime required}"
PYTHON_VERSION=3.13.14
BUILD_VENV="${BUILD_VENV:-/opt/waystone-build-venv}"
MODEL_ROOT="${MODEL_ROOT:-/tmp/waystone-models}"

if [[ "$RUNTIME" != cpu && "$RUNTIME" != nvidia ]]; then
  echo "runtime must be cpu or nvidia" >&2
  exit 2
fi

uv python install "$PYTHON_VERSION"
PYTHON="$(uv python find "$PYTHON_VERSION")"
rm -rf "$BUILD_VENV"
uv venv --python "$PYTHON" "$BUILD_VENV"
uv pip sync --python "$BUILD_VENV/bin/python" "$ROOT/requirements/$RUNTIME.lock"

npm ci --prefix "$ROOT/brain"
npm ci --prefix "$ROOT/brain/vendor/ee2/renderer"

WAYSTONE_OCR_MODEL_ROOT="$MODEL_ROOT" "$ROOT/scripts/fetch-ocr-models"
rm -rf /root/.cache/uv /root/.npm
