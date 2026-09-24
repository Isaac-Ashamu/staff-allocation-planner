#!/usr/bin/env bash
# Stop the osrm-routed instance started by start-osrm.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$REPO_ROOT/local-data/osrm-gb/osrm-routed.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "no pid file at $PID_FILE -- is osrm-routed running under a different launcher?" >&2
  exit 1
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "stopped osrm-routed (pid $PID)"
else
  echo "no running process for pid $PID (already stopped?)"
fi
rm -f "$PID_FILE"
