#!/usr/bin/env bash
# Recurring pricing baseline seeding. Safe to run from cron:
# - flock prevents overlapping runs
# - the module is idempotent: it only seeds pursuits with no simulation yet
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_env.sh"

LOCK="$REPO/var/pricing_seed.lock"
LOG="$REPO/var/pricing_seed.log"
export THAQIP_PRICING_SEED_LIMIT="${THAQIP_PRICING_SEED_LIMIT:-100}"

mkdir -p "$REPO/var"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) pricing seed already running; skipping" >> "$LOG"
  exit 0
fi

echo "$(date -Is) pricing seed session starting" >> "$LOG"
cd "$REPO/services/ingestion"
exec "$UV" run --extra db \
  python -m thaqip_ingestion.pricing_seed --limit "$THAQIP_PRICING_SEED_LIMIT" >> "$LOG" 2>&1
