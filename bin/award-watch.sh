#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_env.sh"
cd "$REPO/services/ingestion"
exec "$UV" run --extra db --extra browser \
  python -m thaqip_ingestion.award_watch >> "$REPO/var/award_watch.log" 2>&1
