#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec node "$ROOT/native_agent/runtime/pair_dialogue.mjs" "$@"
