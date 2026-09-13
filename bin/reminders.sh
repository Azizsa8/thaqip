#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_env.sh"
cd "$REPO/services/ingestion"
exec "$UV" run --extra db python -m thaqip_ingestion.reminders >> "$REPO/var/reminders.log" 2>&1
