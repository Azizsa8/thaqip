#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export DATABASE_URL="${DATABASE_URL:-postgres://thaqip:thaqip_dev@localhost:5433/thaqip}"
cd "$REPO/services/ingestion"
exec uv run --extra db python -m thaqip_ingestion.reminders >> "$REPO/var/reminders.log" 2>&1
