#!/bin/sh
set -eu

server_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$server_dir/../.." && pwd)

: "${DOGOS_MEMORY_SOURCE:=$project_root}"
export DOGOS_MEMORY_SOURCE

exec node "$server_dir/server.mjs"
