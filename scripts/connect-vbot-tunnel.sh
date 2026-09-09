#!/usr/bin/env bash
set -euo pipefail

usage() {
  /bin/cat <<'USAGE'
Usage:
  connect-vbot-tunnel.sh --ssh-config FILE --host SSH_ALIAS --local-port PORT [options]

Starts one fixed Vbot identity tunnel and reconnects after a link failure.
Run one process for dog A (normally port 19091) and one for dog B (19092).
The script starts only the read-only identity bridge; it does not send robot actions.

Options:
  --dry-run              Print the fixed route without contacting a robot.
  --max-attempts N       Stop after N attempts; 0 retries forever (default: 0).
  --retry-delay SECONDS  Delay before reconnecting (default: 2).
USAGE
}

dry_run=false
ssh_config=""
ssh_host=""
local_port=""
max_attempts=0
retry_delay=2

while (( $# > 0 )); do
  case "$1" in
    --help)
      usage
      exit 0
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    --ssh-config)
      ssh_config="${2:-}"
      shift 2
      ;;
    --host)
      ssh_host="${2:-}"
      shift 2
      ;;
    --local-port)
      local_port="${2:-}"
      shift 2
      ;;
    --max-attempts)
      max_attempts="${2:-}"
      shift 2
      ;;
    --retry-delay)
      retry_delay="${2:-}"
      shift 2
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$ssh_config" || ! "$ssh_host" =~ ^[A-Za-z0-9._-]+$ ]]; then
  usage >&2
  exit 2
fi
if [[ ! "$local_port" =~ ^[0-9]+$ ]] || (( local_port < 1024 || local_port > 65535 )); then
  usage >&2
  exit 2
fi
if [[ ! "$max_attempts" =~ ^[0-9]+$ || ! "$retry_delay" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  usage >&2
  exit 2
fi

remote_bridge="/userdata/vbot/dogos-identity/current/vbot_identity_bridge.py"
forward="127.0.0.1:${local_port}:127.0.0.1:9092"

if [[ "$dry_run" == true ]]; then
  /bin/echo "ssh -F $ssh_config $ssh_host start $remote_bridge"
  /bin/echo "ssh -F $ssh_config -N -L $forward $ssh_host"
  exit 0
fi

ssh_options=(
  -F "$ssh_config"
  -o ConnectTimeout=5
  -o ServerAliveInterval=5
  -o ServerAliveCountMax=2
)

start_bridge() {
  ssh "${ssh_options[@]}" "$ssh_host" 'bash -s' <<'REMOTE'
set -eo pipefail
set +u
source /app/opt/ros/humble/setup.bash
source /app/idl_msgs/setup.bash
set -u
export RMW_IMPLEMENTATION=rmw_zenoh_cpp
export ZENOH_SESSION_CONFIG_URI=/app_param/zenoh/s100_session.json5
python3 - <<'PY'
import pathlib
import subprocess

root = pathlib.Path('/userdata/vbot/dogos-identity')
bridge = root / 'current' / 'vbot_identity_bridge.py'
pid_file = root / 'identity.pid'
if not bridge.exists():
    raise SystemExit(f'Adapter not installed: {bridge}')
try:
    pid = int(pid_file.read_text())
    running = bridge.as_posix().encode() in pathlib.Path(f'/proc/{pid}/cmdline').read_bytes()
except (OSError, ValueError):
    running = False
if not running:
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
}

stop_requested=0
trap 'stop_requested=1' INT TERM
attempt=1
last_status=1

while (( max_attempts == 0 || attempt <= max_attempts )); do
  if start_bridge; then
    if ssh "${ssh_options[@]}" -o ExitOnForwardFailure=yes -N -L "$forward" "$ssh_host"; then
      last_status=0
    else
      last_status=$?
    fi
  else
    last_status=$?
  fi

  if (( stop_requested == 1 )); then
    exit 130
  fi
  if (( max_attempts > 0 && attempt >= max_attempts )); then
    break
  fi
  /bin/echo "Tunnel for $ssh_host disconnected; reconnecting in ${retry_delay}s" >&2
  /bin/sleep "$retry_delay"
  ((attempt += 1))
done

exit "$last_status"
