#!/usr/bin/env bash
# Historical awards backfill (deep lane). Cron runs it hourly; each session
# walks older awarded pages for at most 45 minutes from its checkpoint, then
# exits so Etimad's WAF gets a rest. Shares one lock with awards-harvest.sh so
# the two lanes never scrape at the same time.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_env.sh"

LOCK="$REPO/var/etimad_scrape.lock"
LOG="$REPO/var/awards_backfill.log"

mkdir -p "$REPO/var"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) etimad scrape lock busy; backfill session skipped" >> "$LOG"
  exit 0
fi

echo "$(date -Is) backfill session starting" >> "$LOG"
cd "$REPO/services/ingestion"
exec "$UV" run --extra db --extra browser \
  python -m thaqip_ingestion.awards_harvest --mode backfill --pages 2000 --page-size 20 \
  --max-minutes "${THAQIP_BACKFILL_SESSION_MINUTES:-45}" >> "$LOG" 2>&1
