#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DOGOS_ROOT=${DOGOS_MEMORY_SOURCE:-"$ROOT"}
PID_FILE="$DOGOS_ROOT/data/two_dog_link.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Two-dog link is not running."
  exit 0
fi
pid=$(cat "$PID_FILE")
if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
  echo "Invalid two-dog link PID file." >&2
  exit 2
fi
command=$(ps -p "$pid" -o command= 2>/dev/null || true)
if [[ "$command" != *"scripts/start-two-dog-link.sh"* ]]; then
  echo "PID $pid is not the two-dog link process; refusing to stop it." >&2
  exit 2
fi
kill -TERM "$pid"
for _ in {1..50}; do
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.1
done
if kill -0 "$pid" 2>/dev/null; then
  echo "Two-dog link did not stop cleanly." >&2
  exit 1
fi
rm -f "$PID_FILE"
echo "Two-dog link stopped."
