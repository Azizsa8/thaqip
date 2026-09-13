# Sourced by every bin/ script. One place that knows how a host script reaches
# the stack, so a server with generated secrets needs no script edits.
#   - ./.env (gitignored) holds POSTGRES_PASSWORD etc. on servers
#   - UV is found on PATH, else the per-user install location
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/.env"
  set +a
fi
export DATABASE_URL="${DATABASE_URL:-postgres://thaqip:${POSTGRES_PASSWORD:-thaqip_dev}@localhost:${THAQIP_PG_HOST_PORT:-5433}/thaqip}"
UV="${UV:-$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")}"
export UV
