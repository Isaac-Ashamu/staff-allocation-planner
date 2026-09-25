#!/usr/bin/env bash
# Stop an osrm-routed instance started by start-osrm.sh.
#
# Usage: scripts/stop-osrm.sh [drive|cycle|walk]   (default: drive)
set -euo pipefail

PROFILE="${1:-drive}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$PROFILE" in
  drive) OSRM_DIR="$REPO_ROOT/local-data/osrm-gb" ;;
  cycle) OSRM_DIR="$REPO_ROOT/local-data/osrm-gb-bicycle" ;;
  walk)  OSRM_DIR="$REPO_ROOT/local-data/osrm-gb-foot" ;;
  *)
    echo "error: unknown profile '$PROFILE' -- expected drive, cycle, or walk" >&2
    exit 2
    ;;
esac

PID_FILE="$OSRM_DIR/osrm-routed.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "no pid file at $PID_FILE -- is osrm-routed ($PROFILE) running under a different launcher?" >&2
  exit 1
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "stopped osrm-routed ($PROFILE, pid $PID)"
else
  echo "no running process for pid $PID (already stopped?)"
fi
rm -f "$PID_FILE"
