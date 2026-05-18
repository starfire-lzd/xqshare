#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SERVER_HOST="${1:-${XQSHARE_REMOTE_HOST:-xqshare-server}}"
PORT="${2:-${XQSHARE_REMOTE_PORT:-18812}}"

python -m pip install -e .
# The Tailscale sidecar is prebuilt and packaged under xqshare/bin.

export XQSHARE_TAILSCALE=1
export XQSHARE_REMOTE_HOST="$SERVER_HOST"
export XQSHARE_REMOTE_PORT="$PORT"
export XQSHARE_TS_TARGET_HOST="$SERVER_HOST"
export XQSHARE_TS_TARGET_PORT="$PORT"
export XQSHARE_TS_LOCAL_HOST="${XQSHARE_TS_LOCAL_HOST:-127.0.0.1}"
export XQSHARE_TS_LOCAL_PORT="${XQSHARE_TS_LOCAL_PORT:-$PORT}"

python - <<'PY'
from xqshare import XtQuantRemote

xt = XtQuantRemote()
try:
    print(xt.get_service_status())
finally:
    xt.close()
PY
