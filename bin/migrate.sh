#!/usr/bin/env bash
# Apply pending SQL migrations from db/migrations to the target database.
#
# The postgres container only executes db/migrations on FIRST init
# (docker-entrypoint-initdb.d), so this is the supported way to move an existing
# database forward. Idempotent: re-running applies nothing.
#
#   bin/migrate.sh                 # apply pending migrations
#   bin/migrate.sh --dry-run       # show what would run
#   bin/migrate.sh --baseline      # record pending files WITHOUT executing them
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
export DATABASE_URL="${DATABASE_URL:-postgres://thaqip:thaqip_dev@localhost:5433/thaqip}"

cd "$REPO/services/ingestion"
exec /home/ais04/.local/bin/uv run --extra db \
  python -m thaqip_ingestion.migrate --dir "$REPO/db/migrations" "$@"
