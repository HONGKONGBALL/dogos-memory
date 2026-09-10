#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DOGOS_ROOT=${DOGOS_MEMORY_SOURCE:-"$ROOT"}
SSH_CONFIG=${DOGOS_SSH_CONFIG:-"$DOGOS_ROOT/data/two_vbots.ssh_config"}
CONNECTION_CONFIG=${DOGOS_CONNECTION_CONFIG:-"$DOGOS_ROOT/data/two_vbots.connections.json"}
SOURCE_REVISION_FILE="$DOGOS_ROOT/.source-revision"
PID_FILE="$DOGOS_ROOT/data/two_dog_link.pid"

inventory_mode=false
case "${1:-}" in
  "") ;;
  --inventory) inventory_mode=true; shift ;;
  -h|--help)
    echo "Usage: $0 [--inventory]"
    echo "  --inventory  Probe two pre-labeled physical routes and print IDs without binding them."
    exit 0
    ;;
  *)
    echo "Usage: $0 [--inventory]" >&2
    exit 2
    ;;
esac
if (( $# != 0 )); then
  echo "Usage: $0 [--inventory]" >&2
  exit 2
fi

for required in "$DOGOS_ROOT/pyproject.toml" "$SOURCE_REVISION_FILE" "$SSH_CONFIG" "$CONNECTION_CONFIG"; do
  if [[ ! -f "$required" ]]; then
    echo "Missing required file: $required" >&2
    exit 2
  fi
done
if grep -q 'REPLACE_WITH_' "$SSH_CONFIG"; then
  echo "Fill two physical Vbots' distinct routes first." >&2
  exit 2
fi
if [[ "$inventory_mode" == false ]] && \
  grep -Eq 'REPLACE_WITH_|vbot-aaaaaaaaaaaaaaaa|vbot-bbbbbbbbbbbbbbbb' "$CONNECTION_CONFIG"; then
  echo "Run $0 --inventory, map each printed ID to its physical label, then update $CONNECTION_CONFIG." >&2
  exit 2
fi

vbot_a_hostname=$(ssh -G -F "$SSH_CONFIG" vbot-a 2>/dev/null | awk '$1 == "hostname" { print $2; exit }')
vbot_b_hostname=$(ssh -G -F "$SSH_CONFIG" vbot-b 2>/dev/null | awk '$1 == "hostname" { print $2; exit }')
if [[ -z "$vbot_a_hostname" || -z "$vbot_b_hostname" ]]; then
  echo "Both vbot-a and vbot-b must resolve to explicit HostName values." >&2
  exit 2
fi
if [[ "$vbot_a_hostname" == "$vbot_b_hostname" ]]; then
  echo "vbot-a and vbot-b resolve to the same HostName ($vbot_a_hostname)." >&2
  echo "Inventory two physical Vbots and assign distinct reachable addresses before pairing." >&2
  exit 2
fi

if [[ -f "$PID_FILE" ]]; then
  existing_pid=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "Two-dog link is already running as PID $existing_pid." >&2
    exit 2
  fi
fi

for port in 19091 19092; do
  if nc -z 127.0.0.1 "$port" >/dev/null 2>&1; then
    echo "Local port $port is already in use; stop the old tunnel first." >&2
    exit 2
  fi
done

socket_dir=$(mktemp -d "${TMPDIR:-/tmp}/dogos-ssh.XXXXXX")
report_file=$(mktemp "${TMPDIR:-/tmp}/dogos-connect.XXXXXX")
mcp_pid=""
cleanup() {
  if [[ -n "$mcp_pid" ]]; then
    kill "$mcp_pid" 2>/dev/null || true
    wait "$mcp_pid" 2>/dev/null || true
  fi
  for host in vbot-a vbot-b; do
    socket="$socket_dir/$host.sock"
    if [[ -S "$socket" ]]; then
      ssh -F "$SSH_CONFIG" -S "$socket" "$host" 'bash -s' >/dev/null 2>&1 <<'REMOTE' || true
python3 - <<'PY'
import os
import pathlib
import signal
import time

root = pathlib.Path('/userdata/vbot/dogos-identity')
pid_file = root / 'identity.pid'
try:
    pid = int(pid_file.read_text())
    command = pathlib.Path(f'/proc/{pid}/cmdline').read_bytes()
    owned = (
        b'/userdata/vbot/dogos-identity/' in command
        and b'vbot_identity_bridge.py' in command
    )
    if owned:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not pathlib.Path(f'/proc/{pid}').exists():
                break
            time.sleep(0.1)
except (OSError, ValueError):
    pass
PY
REMOTE
      ssh -F "$SSH_CONFIG" -S "$socket" -O exit "$host" >/dev/null 2>&1 || true
    fi
  done
  rm -rf "$socket_dir"
  rm -f "$report_file"
  if [[ $(cat "$PID_FILE" 2>/dev/null || true) == "$$" ]]; then
    rm -f "$PID_FILE"
  fi
}
trap cleanup EXIT INT TERM
printf '%s\n' "$$" >"$PID_FILE"
chmod 600 "$PID_FILE"

use_password=false
for host in vbot-a vbot-b; do
  # The dedicated tunnels below use new control sockets. An existing master
  # must not make this probe falsely report that fresh key login will work.
  if ! ssh -F "$SSH_CONFIG" -o ControlPath=none -o ControlMaster=no \
    -o BatchMode=yes -o ConnectTimeout=3 "$host" true >/dev/null 2>&1; then
    use_password=true
    break
  fi
done
ssh_password=""
if [[ "$use_password" == true ]]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    echo "sshpass is required for one-prompt password login." >&2
    exit 2
  fi
  read -r -s -p "Vbot SSH password (kept only in this process): " ssh_password
  echo
fi

run_login() {
  if [[ "$use_password" == true ]]; then
    sshpass -d 3 "$@" 3<<<"$ssh_password"
  else
    "$@"
  fi
}

for host in vbot-a vbot-b; do
  run_login ssh -F "$SSH_CONFIG" \
    -o ControlMaster=yes -o ControlPath="$socket_dir/$host.sock" -o ControlPersist=no \
    -o ExitOnForwardFailure=yes -fN "$host"
done
unset ssh_password

release="release-$(cut -c1-12 "$SOURCE_REVISION_FILE")"
for host in vbot-a vbot-b; do
  socket="$socket_dir/$host.sock"
  remote_root="/userdata/vbot/dogos-identity/$release"
  ssh -F "$SSH_CONFIG" -S "$socket" "$host" "mkdir -p '$remote_root/dogos_demo'"
  scp -q -F "$SSH_CONFIG" -o ControlPath="$socket" \
    "$DOGOS_ROOT/robot_side/vbot_identity_bridge.py" "$host:$remote_root/"
  scp -q -F "$SSH_CONFIG" -o ControlPath="$socket" \
    "$DOGOS_ROOT/dogos_demo/__init__.py" \
    "$DOGOS_ROOT/dogos_demo/vbot_identity_core.py" \
    "$DOGOS_ROOT/dogos_demo/vbot_identity_protocol.py" \
    "$host:$remote_root/dogos_demo/"
  ssh -F "$SSH_CONFIG" -S "$socket" "$host" \
    "cd /userdata/vbot/dogos-identity && chmod 755 '$release/vbot_identity_bridge.py' && ln -sfn '$release' current"
  ssh -F "$SSH_CONFIG" -S "$socket" "$host" 'bash -s' <<'REMOTE'
set -eo pipefail
set +u
source /app/opt/ros/humble/local_setup.bash >/dev/null 2>&1
source /app/idl_msgs/local_setup.bash >/dev/null 2>&1
set -u
export RMW_IMPLEMENTATION=rmw_zenoh_cpp
export ZENOH_SESSION_CONFIG_URI=/app_param/zenoh/s100_session.json5
python3 - <<'PY'
import pathlib
import signal
import subprocess
import time

root = pathlib.Path('/userdata/vbot/dogos-identity')
bridge = root / 'current' / 'vbot_identity_bridge.py'
pid_file = root / 'identity.pid'
try:
    pid = int(pid_file.read_text())
    command = pathlib.Path(f'/proc/{pid}/cmdline').read_bytes()
    if b'/userdata/vbot/dogos-identity/' in command:
        pathlib.Path(f'/proc/{pid}').exists() and __import__('os').kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not pathlib.Path(f'/proc/{pid}').exists():
                break
            time.sleep(0.1)
except (OSError, ValueError):
    pass
with (root / 'identity.log').open('a') as log:
    process = subprocess.Popen(
        ['python3', str(bridge)],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
pid_file.write_text(str(process.pid))
PY
REMOTE
  bridge_ready=false
  for _ in {1..60}; do
    if ssh -F "$SSH_CONFIG" -S "$socket" "$host" \
      "python3 -c \"import socket; s=socket.socket(); s.settimeout(.2); raise SystemExit(s.connect_ex(('127.0.0.1',9092)))\"" \
      >/dev/null 2>&1; then
      bridge_ready=true
      break
    fi
    sleep 0.25
  done
  if [[ "$bridge_ready" != true ]]; then
    echo "Identity bridge did not become ready on $host." >&2
    exit 3
  fi
done

ssh -F "$SSH_CONFIG" -S "$socket_dir/vbot-a.sock" -O forward \
  -L 127.0.0.1:19091:127.0.0.1:9092 vbot-a
ssh -F "$SSH_CONFIG" -S "$socket_dir/vbot-b.sock" -O forward \
  -L 127.0.0.1:19092:127.0.0.1:9092 vbot-b

for port in 19091 19092; do
  for _ in {1..40}; do
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1 && break
    sleep 0.1
  done
done

set +e
(cd "$DOGOS_ROOT" && uv run python -m dogos_demo connect-check --config "$CONNECTION_CONFIG") >"$report_file"
connect_status=$?
set -e
cat "$report_file"

if [[ "$inventory_mode" == true ]]; then
  if python3 - "$report_file" <<'PY'
import json
import pathlib
import sys

report = json.loads(pathlib.Path(sys.argv[1]).read_text())
bindings = []
for item in report.get('dogs', []):
    observed = item.get('observed_robot_id')
    if observed is None:
        raise SystemExit(3)
    bindings.append(
        {
            'physical_route': item['dog_id'],
            'adapter_id': item['adapter_id'],
            'observed_robot_id': observed,
        }
    )
if len(bindings) != 2 or len({item['observed_robot_id'] for item in bindings}) != 2:
    raise SystemExit(3)
print(json.dumps({'inventory': 'manual_mapping_required', 'bindings': bindings}, indent=2))
PY
  then
    echo "Confirm each route against its physical body label, then copy the two IDs into $CONNECTION_CONFIG."
    exit 0
  fi
  echo "Two distinct physical routes were not verified; inventory failed closed." >&2
  exit 3
fi

if (( connect_status != 0 )); then
  echo "Pre-mapped identities did not both match; refusing automatic enrollment." >&2
  echo "Map each ID to a physically labeled Vbot, update the config, and run again." >&2
  exit "$connect_status"
fi

echo "Two hardware identities are ready. Starting text/memory MCP endpoints in simulation evidence mode."
DOGOS_MEMORY_SOURCE="$DOGOS_ROOT" \
DOGOS_PAIR_DATA_DIR="$DOGOS_ROOT/data/mcp-pair" \
DOGOS_MODE=simulation \
  "$ROOT/native_agent/dogos_mcp/start_pair.sh" &
mcp_pid=$!
wait "$mcp_pid"
