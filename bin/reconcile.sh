#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_env.sh"
cd "$REPO/services/ingestion"
exec "$UV" run --extra db python -m thaqip_ingestion.reconcile --pages 8 >> "$REPO/var/reconcile.log" 2>&1
