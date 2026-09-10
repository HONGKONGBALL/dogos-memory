#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec node "$ROOT/packages/native_agent/runtime/pair_dialogue.mjs" "--single=${VBOT_DOG_ID:-dog_b}"
