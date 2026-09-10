#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$ROOT/packages${PYTHONPATH:+:$PYTHONPATH}"
COMMAND=${1:-}
if [[ -n "$COMMAND" ]]; then shift; fi

IDENTITY_DIR=${VBOT_IDENTITY_DIR:-}
DOG_ID=${VBOT_DOG_ID:-}
REVISION=""
MINIMUM_BATTERY=${VBOT_MINIMUM_BATTERY:-40}
VBOT_HOST=${VBOT_HOST:-}
VBOT_USER=${VBOT_USER:-vbot}
KNOWN_HOSTS=${VBOT_SSH_KNOWN_HOSTS:-"$ROOT/data/vbot_known_hosts"}

usage() {
  cat <<'EOF'
Usage:
  ./scripts/vbot-native-agent.sh inspect
  ./scripts/vbot-native-agent.sh voice-start --dog-id ID
  ./scripts/vbot-native-agent.sh install --identity-dir DIR --dog-id ID
  ./scripts/vbot-native-agent.sh start --identity-dir DIR --dog-id ID [--minimum-battery 40]
  ./scripts/vbot-native-agent.sh enable --dog-id ID --revision REVISION [--minimum-battery 40]
  ./scripts/vbot-native-agent.sh disable [--dog-id ID]
  ./scripts/vbot-native-agent.sh restore --dog-id ID
  ./scripts/vbot-native-agent.sh rollback --dog-id ID --revision REVISION

The SSH password is requested interactively and is never stored by this script.
"start" installs the identity, verifies the X5 harness, then enables local
wake-word chat in identity-only mode. It never sends a body action.
"voice-start" enables the existing factory identity without installing a SOUL.
Successful switch readback still needs a spoken wake/ASR/reply test on the dog.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --identity-dir) IDENTITY_DIR=${2:-}; shift 2 ;;
    --dog-id) DOG_ID=${2:-}; shift 2 ;;
    --revision) REVISION=${2:-}; shift 2 ;;
    --minimum-battery) MINIMUM_BATTERY=${2:-}; shift 2 ;;
    --host) VBOT_HOST=${2:-}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$COMMAND" in
  inspect|install|start|enable|voice-start|disable|restore|rollback) ;;
  ""|-h|--help) usage; exit 0 ;;
  *) echo "Unknown command: $COMMAND" >&2; usage >&2; exit 2 ;;
esac

if [[ -n "$DOG_ID" && ! "$DOG_ID" =~ ^[a-z0-9_-]{1,64}$ ]]; then
  echo "dog id must contain only lowercase letters, digits, underscore, or hyphen" >&2
  exit 2
fi
if [[ -z "$VBOT_HOST" ]]; then
  echo "VBOT_HOST is required; set it to the device address" >&2
  exit 2
fi
if [[ ! "$VBOT_USER" =~ ^[a-zA-Z0-9._-]{1,32}$ ]]; then
  echo "SSH user contains unsupported characters" >&2
  exit 2
fi
if [[ "$COMMAND" == install || "$COMMAND" == start ]]; then
  if [[ -z "$IDENTITY_DIR" || -z "$DOG_ID" ]]; then
    echo "install/start require --identity-dir and --dog-id" >&2
    exit 2
  fi
  if [[ ! -d "$IDENTITY_DIR" ]]; then
    echo "identity directory does not exist: $IDENTITY_DIR" >&2
    exit 2
  fi
fi
if [[ "$COMMAND" == restore && -z "$DOG_ID" ]]; then
  echo "restore requires --dog-id" >&2
  exit 2
fi
if [[ "$COMMAND" == voice-start && -z "$DOG_ID" ]]; then
  echo "voice-start requires --dog-id" >&2
  exit 2
fi
if [[ "$COMMAND" == enable || "$COMMAND" == rollback ]]; then
  if [[ -z "$DOG_ID" || ! "$REVISION" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]]; then
    echo "$COMMAND requires a valid --dog-id and --revision" >&2
    exit 2
  fi
fi
python3 - "$MINIMUM_BATTERY" <<'PY'
import math, sys
try:
    value = float(sys.argv[1])
except ValueError:
    raise SystemExit("minimum battery must be numeric")
if not math.isfinite(value) or not 0 <= value <= 100:
    raise SystemExit("minimum battery must be between 0 and 100")
PY

RUN_BASE=${TMPDIR:-/tmp}
RUN_ROOT=$(mktemp -d "$RUN_BASE/vbot-native-agent.XXXXXX")
CONNECTED=false
REMOTE_FILES=()
CONTROL_PATH=${VBOT_SSH_CONTROL_PATH:-/tmp/vbot-native-agent-%C}
SSH_ARGS=(-F /dev/null -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KNOWN_HOSTS" -o ConnectTimeout=8 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 -o ControlMaster=auto -o ControlPersist=45 -o "ControlPath=$CONTROL_PATH")
REMOTE_ROOT=/userdata/vbot/native-agent-admin
REMOTE_INBOX=$REMOTE_ROOT/inbox

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  python3 - "$RUN_ROOT" "$RUN_BASE" <<'PY' >/dev/null 2>&1 || true
import shutil
import sys
from pathlib import Path

path = Path(sys.argv[1])
base = Path(sys.argv[2]).resolve()
if path.name.startswith("vbot-native-agent.") and path.parent.resolve() == base:
    shutil.rmtree(path)
PY
  if $CONNECTED && [[ ${#REMOTE_FILES[@]} -gt 0 ]]; then
    ssh "${SSH_ARGS[@]}" "$VBOT_USER@$VBOT_HOST" rm -f -- "${REMOTE_FILES[@]}" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 python3 -m native_agent.runtime.link_check --host "$VBOT_HOST"
if ! ssh-keygen -F "$VBOT_HOST" -f "$KNOWN_HOSTS" >/dev/null; then
  echo "No pinned SSH host key for $VBOT_HOST in $KNOWN_HOSTS" >&2
  exit 2
fi

if [[ "$COMMAND" == install || "$COMMAND" == start ]]; then
  PYTHONDONTWRITEBYTECODE=1 python3 -m native_agent.runtime prepare \
    --identity-dir "$IDENTITY_DIR" --output-dir "$RUN_ROOT/release" --dog-id "$DOG_ID" >/dev/null
else
  PYTHONDONTWRITEBYTECODE=1 python3 -m native_agent.runtime build-admin \
    --output "$RUN_ROOT/s100-admin.pyz" >/dev/null
  if [[ "$COMMAND" == rollback ]]; then
    PYTHONDONTWRITEBYTECODE=1 python3 -m native_agent.runtime build-x5-installer \
      --output "$RUN_ROOT/x5-installer.py" >/dev/null
  fi
fi

ssh "${SSH_ARGS[@]}" "$VBOT_USER@$VBOT_HOST" "mkdir -p '$REMOTE_INBOX' '$REMOTE_ROOT/state' && chmod 700 '$REMOTE_ROOT' '$REMOTE_INBOX' '$REMOTE_ROOT/state'"
CONNECTED=true

if [[ "$COMMAND" == install || "$COMMAND" == start ]]; then
  ADMIN_LOCAL=$RUN_ROOT/release/s100-admin.pyz
  ENVELOPE_LOCAL=$RUN_ROOT/release/identity-envelope.json
  X5_LOCAL=$RUN_ROOT/release/x5-installer.py
  REVISION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["revision"])' "$RUN_ROOT/release/release.json")
  ENVELOPE_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["envelope"]["sha256"])' "$RUN_ROOT/release/release.json")
  ADMIN_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["s100_admin"]["sha256"])' "$RUN_ROOT/release/release.json")
  X5_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["x5_installer"]["sha256"])' "$RUN_ROOT/release/release.json")
else
  ADMIN_LOCAL=$RUN_ROOT/s100-admin.pyz
  ADMIN_SHA=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$ADMIN_LOCAL")
fi
if [[ ! "$ADMIN_SHA" =~ ^[0-9a-f]{64}$ ]]; then
  echo "generated S100 administrator digest is invalid" >&2
  exit 2
fi
if [[ "$COMMAND" == install || "$COMMAND" == start ]]; then
  if [[ ! "$ENVELOPE_SHA" =~ ^[0-9a-f]{64}$ || ! "$X5_SHA" =~ ^[0-9a-f]{64}$ || ! "$REVISION" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]]; then
    echo "generated release metadata is invalid" >&2
    exit 2
  fi
fi

REMOTE_ADMIN=$REMOTE_INBOX/admin-${ADMIN_SHA:0:16}.pyz
scp "${SSH_ARGS[@]}" "$ADMIN_LOCAL" "$VBOT_USER@$VBOT_HOST:$REMOTE_ADMIN" >/dev/null
REMOTE_FILES+=("$REMOTE_ADMIN")

remote_admin() {
  ssh "${SSH_ARGS[@]}" "$VBOT_USER@$VBOT_HOST" bash -s -- "$REMOTE_ADMIN" "$ADMIN_SHA" "$@" <<'REMOTE'
set -euo pipefail
admin=$1
expected=$2
shift 2
actual=$(sha256sum "$admin" | cut -d ' ' -f 1)
test "$actual" = "$expected"
set +u
source /app/opt/ros/humble/local_setup.bash
source /app/idl_msgs/local_setup.bash
set -u
export RMW_IMPLEMENTATION=rmw_zenoh_cpp
export ZENOH_SESSION_CONFIG_URI=/app_param/zenoh/s100_session.json5
exec python3 "$admin" "$@"
REMOTE
}

case "$COMMAND" in
  inspect)
    remote_admin inspect --purpose inspect --minimum-battery "$MINIMUM_BATTERY"
    ;;
  enable)
    remote_admin enable --dog-id "$DOG_ID" --revision "$REVISION" \
      --minimum-battery "$MINIMUM_BATTERY"
    ;;
  voice-start)
    remote_admin voice-start --dog-id "$DOG_ID" --minimum-battery "$MINIMUM_BATTERY"
    ;;
  install|start)
    REMOTE_ENVELOPE=$REMOTE_INBOX/envelope-$REVISION.json
    REMOTE_X5=$REMOTE_INBOX/x5-${X5_SHA:0:16}.py
    scp "${SSH_ARGS[@]}" "$ENVELOPE_LOCAL" "$VBOT_USER@$VBOT_HOST:$REMOTE_ENVELOPE" >/dev/null
    scp "${SSH_ARGS[@]}" "$X5_LOCAL" "$VBOT_USER@$VBOT_HOST:$REMOTE_X5" >/dev/null
    REMOTE_FILES+=("$REMOTE_ENVELOPE" "$REMOTE_X5")
    remote_admin inspect --purpose install --minimum-battery "$MINIMUM_BATTERY"
    remote_admin install --envelope "$REMOTE_ENVELOPE" --envelope-sha256 "$ENVELOPE_SHA" \
      --capsule "$REMOTE_X5" \
      --capsule-sha256 "$X5_SHA" --dog-id "$DOG_ID" --revision "$REVISION"
    if [[ "$COMMAND" == start ]]; then
      remote_admin enable --dog-id "$DOG_ID" --revision "$REVISION" \
        --minimum-battery "$MINIMUM_BATTERY"
    fi
    ;;
  disable)
    if [[ -n "$DOG_ID" ]]; then
      remote_admin disable --dog-id "$DOG_ID"
    else
      remote_admin disable
    fi
    ;;
  restore)
    remote_admin restore --dog-id "$DOG_ID"
    ;;
  rollback)
    X5_LOCAL=$RUN_ROOT/x5-installer.py
    X5_SHA=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$X5_LOCAL")
    REMOTE_X5=$REMOTE_INBOX/x5-${X5_SHA:0:16}.py
    scp "${SSH_ARGS[@]}" "$X5_LOCAL" "$VBOT_USER@$VBOT_HOST:$REMOTE_X5" >/dev/null
    REMOTE_FILES+=("$REMOTE_X5")
    remote_admin rollback --capsule "$REMOTE_X5" --capsule-sha256 "$X5_SHA" \
      --dog-id "$DOG_ID" --revision "$REVISION"
    ;;
esac
