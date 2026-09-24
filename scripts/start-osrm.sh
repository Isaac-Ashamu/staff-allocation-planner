#!/usr/bin/env bash
# Start the self-hosted OSRM driving router used by --live-travel (OSRM_BASE_URL).
#
# Requires the GB extract to already be built under local-data/osrm-gb/ (osrm-extract,
# osrm-partition, osrm-customize against great-britain-latest.osm.pbf with the MLD
# algorithm). This script only starts osrm-routed against that existing dataset; it
# does not build it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OSRM_DIR="$REPO_ROOT/local-data/osrm-gb"
OSRM_FILE="great-britain-latest.osrm"
PORT="${OSRM_PORT:-5001}"
PID_FILE="$OSRM_DIR/osrm-routed.pid"
LOG_FILE="$OSRM_DIR/osrm-routed.log"

if [[ ! -f "$OSRM_DIR/$OSRM_FILE.properties" ]]; then
  # osrm-routed never opens the bare "<name>.osrm" file itself, only its
  # "<name>.osrm.*" companions (ramIndex, fileIndex, properties, ...) -- check
  # for one of those instead of the base name.
  echo "error: $OSRM_DIR/$OSRM_FILE.properties not found -- run osrm-extract/osrm-partition/osrm-customize first" >&2
  exit 1
fi

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "osrm-routed already running (pid $(cat "$PID_FILE"), port $PORT)"
  exit 0
fi

if lsof -i ":$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "error: port $PORT is already in use by another process" >&2
  exit 1
fi

(
  # osrm-routed's argument validator mishandles an absolute --base path that
  # contains spaces (this repo lives under ".../AI projects/..."), so run
  # from the data directory and pass a relative filename instead.
  cd "$OSRM_DIR"
  exec nohup osrm-routed --algorithm MLD --port "$PORT" --mmap=1 "$OSRM_FILE" >>"$LOG_FILE" 2>&1 &
)
sleep 0.2
NEW_PID="$(pgrep -n -f "osrm-routed --algorithm MLD --port $PORT")"
echo "$NEW_PID" >"$PID_FILE"
sleep 1

if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "error: osrm-routed failed to start -- see $LOG_FILE" >&2
  exit 1
fi

echo "osrm-routed started (pid $(cat "$PID_FILE"), port $PORT)"
echo "export OSRM_BASE_URL=\"http://127.0.0.1:$PORT\""
