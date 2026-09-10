#!/bin/sh
set -eu

server_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$server_dir/../.." && pwd)
dogos_source=${DOGOS_MEMORY_SOURCE:-"$project_root"}
data_dir=${DOGOS_PAIR_DATA_DIR:-"$dogos_source/data/mcp-pair"}
python_bin=${DOGOS_PYTHON:-python3}

mkdir -p "$data_dir"
"$python_bin" "$server_dir/bootstrap_pair.py" \
  --dogos-source "$dogos_source" --directory "$data_dir" --mode "${DOGOS_MODE:-simulation}" >/dev/null

start_endpoint() {
  owner=$1
  peer=$2
  port=$3
  DOGOS_MEMORY_SOURCE="$dogos_source" \
  DOGOS_DATABASE="$data_dir/$owner.db" \
  DOGOS_PEER_DATABASE="$data_dir/$peer.db" \
  DOGOS_RELAY_DATABASE="$data_dir/relay.db" \
  DOGOS_OWNER_ID="$owner" \
  DOGOS_PEER_ID="$peer" \
  DOGOS_MCP_PORT="$port" \
  DOGOS_PYTHON="$python_bin" \
    "$server_dir/start.sh"
}

start_endpoint dog_a dog_b "${DOGOS_A_MCP_PORT:-8768}" &
pid_a=$!
start_endpoint dog_b dog_a "${DOGOS_B_MCP_PORT:-8769}" &
pid_b=$!
trap 'kill "$pid_a" "$pid_b" 2>/dev/null || true; wait "$pid_a" "$pid_b" 2>/dev/null || true' INT TERM EXIT
wait "$pid_a" "$pid_b"
