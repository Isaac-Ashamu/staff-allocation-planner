#!/usr/bin/env bash
# Start a self-hosted OSRM router used by --live-travel.
#
# Usage: scripts/start-osrm.sh [drive|cycle|walk]   (default: drive)
#
# Requires the matching GB extract to already be built (osrm-extract with the
# profile's .lua file, then osrm-partition, then osrm-customize, all with the
# MLD algorithm) under the profile's data directory below. This script only
# starts osrm-routed against an existing dataset; it does not build one.
set -euo pipefail

PROFILE="${1:-drive}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$PROFILE" in
  drive)
    OSRM_DIR="$REPO_ROOT/local-data/osrm-gb"
    DEFAULT_PORT=5001
    ENV_VAR=OSRM_BASE_URL
    ;;
  cycle)
    OSRM_DIR="$REPO_ROOT/local-data/osrm-gb-bicycle"
    DEFAULT_PORT=5002
    ENV_VAR=OSRM_CYCLE_BASE_URL
    ;;
  walk)
    OSRM_DIR="$REPO_ROOT/local-data/osrm-gb-foot"
    DEFAULT_PORT=5003
    ENV_VAR=OSRM_WALK_BASE_URL
    ;;
  *)
    echo "error: unknown profile '$PROFILE' -- expected drive, cycle, or walk" >&2
    exit 2
    ;;
esac

OSRM_FILE="great-britain-latest.osrm"
PORT="${OSRM_PORT:-$DEFAULT_PORT}"
PID_FILE="$OSRM_DIR/osrm-routed.pid"
LOG_FILE="$OSRM_DIR/osrm-routed.log"

if [[ ! -f "$OSRM_DIR/$OSRM_FILE.properties" ]]; then
  # osrm-routed never opens the bare "<name>.osrm" file itself, only its
  # "<name>.osrm.*" companions (ramIndex, fileIndex, properties, ...) -- check
  # for one of those instead of the base name.
  echo "error: $OSRM_DIR/$OSRM_FILE.properties not found -- run osrm-extract/osrm-partition/osrm-customize for the $PROFILE profile first" >&2
  exit 1
fi

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "osrm-routed ($PROFILE) already running (pid $(cat "$PID_FILE"), port $PORT)"
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
  echo "error: osrm-routed ($PROFILE) failed to start -- see $LOG_FILE" >&2
  exit 1
fi

echo "osrm-routed ($PROFILE) started (pid $(cat "$PID_FILE"), port $PORT)"
echo "export $ENV_VAR=\"http://127.0.0.1:$PORT\""
