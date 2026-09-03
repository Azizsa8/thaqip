#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export DATABASE_URL="${DATABASE_URL:-postgres://thaqip:thaqip_dev@localhost:5433/thaqip}"
cd "$REPO/services/ingestion"
exec /home/ais04/.local/bin/uv run --extra db --extra browser \
  python -m thaqip_ingestion.award_watch >> "$REPO/var/award_watch.log" 2>&1
