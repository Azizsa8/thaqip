#!/usr/bin/env bash
# Generate production secrets into ./.env (gitignored, mode 600). Run once on
# a new server, BEFORE the first `docker compose up`: Postgres only reads
# POSTGRES_PASSWORD when it initialises an empty volume.
# Refuses to overwrite an existing .env.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/.env"
[ -e "$OUT" ] && { echo "$OUT exists; not overwriting" >&2; exit 1; }
rand() { head -c 96 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-40}"; }

read -r -s -p "Cloudflare Tunnel token (Zero Trust -> Networks -> Tunnels -> your tunnel; input hidden): " TUNNEL; echo
[ ${#TUNNEL} -gt 50 ] || { echo "that does not look like a tunnel token" >&2; exit 1; }

umask 077
cat > "$OUT" <<ENV
# Generated $(date -u +%FT%TZ) by bin/prod-secrets.sh. Never commit.
COMPOSE_PROJECT_NAME=thaqip
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
POSTGRES_PASSWORD=$(rand 40)
MINIO_ROOT_USER=thaqip-$(rand 8)
MINIO_ROOT_PASSWORD=$(rand 40)
TYPESENSE_API_KEY=$(rand 40)
CLOUDFLARE_TUNNEL_TOKEN=$TUNNEL
ENV
chmod 600 "$OUT"
echo "wrote $OUT"
