#!/bin/sh
# Gas City service entrypoint: serve this town's envoy on $GC_SERVICE_SOCKET.
# Runs with cwd = pack directory; town.toml is derived from GC_SERVICE_STATE_ROOT.
set -eu
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
: "${GC_SERVICE_SOCKET:?GC_SERVICE_SOCKET is required}"
if [ -z "${PT_TOWN_TOML:-}" ] && [ -n "${GC_SERVICE_STATE_ROOT:-}" ]; then
  PT_TOWN_TOML="$(cd "$GC_SERVICE_STATE_ROOT/../../.." && pwd)/town.toml"
  export PT_TOWN_TOML
fi
exec pangenome-town envoy --socket "$GC_SERVICE_SOCKET"
