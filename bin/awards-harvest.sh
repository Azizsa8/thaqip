#!/usr/bin/env bash
# Recurring awards harvest (ticket B4). Safe to run from cron:
# - flock prevents overlapping runs (a long run simply keeps the lock)
# - checkpointing in the harvester resumes from the last completed page
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# Shared with awards-backfill.sh: one Etimad scraper at a time.
LOCK="$REPO/var/etimad_scrape.lock"
LOG="$REPO/var/awards_harvest.log"
export DATABASE_URL="${DATABASE_URL:-postgres://thaqip:thaqip_dev@localhost:5433/thaqip}"

mkdir -p "$REPO/var"
exec 9>"$LOCK"
# Fresh awards matter more than history: wait up to 50 min for a backfill
# session to finish (they are capped at 45) instead of skipping this slot.
if ! flock -w 3000 9; then
  echo "$(date -Is) scrape lock still busy after 50 min; skipping" >> "$LOG"
  exit 0
fi

echo "$(date -Is) cron harvest session starting" >> "$LOG"
cd "$REPO/services/ingestion"
exec /home/ais04/.local/bin/uv run --extra db --extra browser \
  python -m thaqip_ingestion.awards_harvest --mode fresh --pages 10 --page-size 20 >> "$LOG" 2>&1
