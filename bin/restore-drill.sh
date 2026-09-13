#!/usr/bin/env bash
# Restore drill: restore the newest backup into a scratch database, compare
# row counts of the tables that matter with live, then drop the scratch copy.
# A backup is only trusted after this passes. Records ops.restore_drill.
#
#   bin/restore-drill.sh            # newest backup
#   bin/restore-drill.sh <dir>      # a specific backup directory
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_env.sh"

PG="${THAQIP_PG_CONTAINER:-thaqip-postgres-1}"
SRC="${1:-$(ls -1d "$REPO"/var/backups/20* 2>/dev/null | sort | tail -1)}"
SCRATCH="thaqip_restore_drill"
[ -n "$SRC" ] && [ -f "$SRC/thaqip.dump" ] || { echo "no backup found" >&2; exit 1; }
( cd "$SRC" && sha256sum -c --quiet SHA256SUMS )

q() { docker exec "$PG" psql -U thaqip -d "$1" -Atq -v ON_ERROR_STOP=1 -c "$2"; }
RUN_ID="$(q thaqip "INSERT INTO ingest_runs (connector) VALUES ('ops.restore_drill') RETURNING id")"
cleanup() { q thaqip "DROP DATABASE IF EXISTS $SCRATCH" >/dev/null 2>&1 || true; }
trap cleanup EXIT

cleanup
q thaqip "CREATE DATABASE $SCRATCH" >/dev/null
# --no-owner/--no-acl: the drill checks the data, and must not depend on roles.
docker exec -i "$PG" pg_restore -U thaqip -d "$SCRATCH" --no-owner --no-acl --exit-on-error < "$SRC/thaqip.dump"

FAIL=0; REPORT=""
for t in tenders offers awards vendors agencies users sessions pursuits user_bid_scenarios \
         price_predictions prediction_feedback tender_award_first_seen price_indices schema_migrations; do
  restored="$(q "$SCRATCH" "SELECT count(*) FROM $t" 2>/dev/null || echo MISSING)"
  live="$(q thaqip "SELECT count(*) FROM $t" 2>/dev/null || echo MISSING)"
  status=ok
  if [ "$restored" = MISSING ]; then status=MISSING; FAIL=1
  elif [ "$live" != MISSING ] && [ "$restored" -gt "$live" ]; then status="MORE-THAN-LIVE"; FAIL=1
  elif [ "$live" != MISSING ] && [ "$live" -gt 0 ] && [ "$restored" -eq 0 ]; then status=EMPTY; FAIL=1
  fi
  REPORT+=$(printf "%-26s restored=%-8s live=%-8s %s\n" "$t" "$restored" "$live" "$status")$'\n'
done
printf "%s" "$REPORT"
MIG_LIVE="$(q thaqip "SELECT max(filename) FROM schema_migrations")"
echo "backup: $SRC   latest live migration: $MIG_LIVE"

OK=true; [ "$FAIL" -eq 0 ] || OK=false
q thaqip "UPDATE ingest_runs SET finished_at=now(), ok=$OK, error=$( [ "$FAIL" -eq 0 ] && echo NULL || echo "'restore drill found missing or empty tables'") WHERE id=$RUN_ID" >/dev/null
[ "$FAIL" -eq 0 ] && echo "RESTORE DRILL PASSED" || { echo "RESTORE DRILL FAILED" >&2; exit 1; }
