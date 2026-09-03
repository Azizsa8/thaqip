#!/usr/bin/env bash
# Recurring awards harvest (ticket B4). Safe to run from cron:
# - flock prevents overlapping runs (a long run simply keeps the lock)
# - checkpointing in the harvester resumes from the last completed page
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="$REPO/var/awards_harvest.lock"
LOG="$REPO/var/awards_harvest.log"
export DATABASE_URL="${DATABASE_URL:-postgres://thaqip:thaqip_dev@localhost:5433/thaqip}"

mkdir -p "$REPO/var"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) harvest already running; skipping" >> "$LOG"
  exit 0
fi

echo "$(date -Is) cron harvest session starting" >> "$LOG"
cd "$REPO/services/ingestion"
exec /home/ais04/.local/bin/uv run --extra db --extra browser \
  python -m thaqip_ingestion.awards_harvest --pages 100 --page-size 20 >> "$LOG" 2>&1
