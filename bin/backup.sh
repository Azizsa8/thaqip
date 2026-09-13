#!/usr/bin/env bash
# Nightly backup: the corpus database, Superset's metadata database (saved
# charts and dashboards people edited), and cluster roles. Keeps 14 local
# days; if BACKUP_RCLONE_REMOTE is set (e.g. "oci:thaqip-backups"), each run
# is also copied off the machine with rclone. Records the run in ingest_runs
# (connector ops.backup) so the dashboard shows a missed night.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_env.sh"

PG="${THAQIP_PG_CONTAINER:-thaqip-postgres-1}"
DIR="$REPO/var/backups"
STAMP="$(date -u +%Y%m%dT%H%MZ)"
OUT="$DIR/$STAMP"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
umask 077
mkdir -p "$OUT"

psql_q() { docker exec "$PG" psql -U thaqip -d thaqip -Atq -v ON_ERROR_STOP=1 -c "$1"; }
RUN_ID="$(psql_q "INSERT INTO ingest_runs (connector) VALUES ('ops.backup') RETURNING id")"
finish() {
  local ok="$1" msg="$2" size="${3:-0}"
  psql_q "UPDATE ingest_runs SET finished_at=now(), ok=$ok, items_seen=$size,
          error=NULLIF('$(printf %s "$msg" | tr -d "'")', '') WHERE id=$RUN_ID" >/dev/null || true
}
trap 'finish false "backup failed at line $LINENO"' ERR

docker exec "$PG" pg_dump -U thaqip -d thaqip -Fc > "$OUT/thaqip.dump"
if docker exec "$PG" psql -U thaqip -d thaqip -At -c "SELECT 1 FROM pg_database WHERE datname='superset'" | grep -q 1; then
  docker exec "$PG" pg_dump -U thaqip -d superset -Fc > "$OUT/superset.dump"
fi
docker exec "$PG" pg_dumpall -U thaqip --globals-only > "$OUT/globals.sql"

# A dump that pg_restore cannot list is not a backup.
for f in "$OUT"/*.dump; do
  docker exec -i "$PG" pg_restore -l < "$f" > /dev/null
done
( cd "$OUT" && sha256sum ./* > SHA256SUMS )

if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
  rclone copy "$OUT" "$BACKUP_RCLONE_REMOTE/$STAMP" --checksum
fi

find "$DIR" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime +"$KEEP_DAYS" -exec rm -rf {} +
SIZE_KB="$(du -sk "$OUT" | cut -f1)"
finish true "" "$SIZE_KB"
echo "$(date -Is) backup ok: $OUT (${SIZE_KB} KB)${BACKUP_RCLONE_REMOTE:+ + $BACKUP_RCLONE_REMOTE}"
