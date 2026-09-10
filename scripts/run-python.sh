#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$ROOT/packages:$ROOT/apps${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
exec uv run python "$@"
