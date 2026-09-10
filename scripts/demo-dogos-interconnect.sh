#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DOGOS_ROOT=${DOGOS_MEMORY_SOURCE:-"$ROOT"}

usage() {
  cat <<'EOF'
Usage:
  ./scripts/demo-dogos-interconnect.sh simulation
  ./scripts/demo-dogos-interconnect.sh simulation-serve
  ./scripts/demo-dogos-interconnect.sh physical-inventory
  ./scripts/demo-dogos-interconnect.sh physical

simulation        Run one complete A -> B MCP message, acknowledgement, and
                  two-database persistence proof, then stop everything.
simulation-serve  Keep the local dog_a/dog_b MCP endpoints on ports 8768/8769.
physical-inventory
                  Probe two labeled Vbots and print their IDs without saving them.
physical          Require two inventoried physical Vbots, verify both identities,
                  then keep the same two MCP endpoints running.
EOF
}

mode=${1:-simulation}
case "$mode" in
  simulation)
    echo "[SIMULATION] Starting two local DogOS/MCP identities; no robot is controlled." >&2
    DOGOS_MEMORY_SOURCE="$DOGOS_ROOT" \
      exec node "$ROOT/native_agent/dogos_mcp/smoke_test.mjs"
    ;;
  simulation-serve)
    echo "[SIMULATION] dog_a=http://127.0.0.1:8768/mcp dog_b=http://127.0.0.1:8769/mcp" >&2
    DOGOS_MEMORY_SOURCE="$DOGOS_ROOT" DOGOS_MODE=simulation \
      exec "$ROOT/native_agent/dogos_mcp/start_pair.sh"
    ;;
  physical-inventory)
    echo "[PHYSICAL INVENTORY] Requires two labeled Vbots with distinct configured HostName values." >&2
    DOGOS_MEMORY_SOURCE="$DOGOS_ROOT" \
      exec "$ROOT/scripts/start-two-dog-link.sh" --inventory
    ;;
  physical)
    echo "[PHYSICAL] Requires two labeled Vbots with distinct configured HostName values." >&2
    DOGOS_MEMORY_SOURCE="$DOGOS_ROOT" \
      exec "$ROOT/scripts/start-two-dog-link.sh"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
