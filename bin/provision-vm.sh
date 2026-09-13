#!/usr/bin/env bash
# One-shot setup of a fresh Ubuntu 24.04 (arm64 or amd64) server for Thaqip.
# Run as the login user (with sudo), from a clone of the private repo
# (see docs/DEPLOY.md for cloning with a read-only deploy key):
#
#   bin/provision-vm.sh [path/to/backup-dir]
#
# With a backup directory (made by bin/backup.sh on the old host and copied
# over with scp), the database is restored from it; without one, an empty
# database is built from migrations. Re-running skips steps already done.
# Nothing here opens an inbound port: traffic arrives through Cloudflare
# Tunnel, and ufw allows SSH only.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP="${1:-}"
cd "$REPO"
step() { printf '\n== %s\n' "$*"; }

step "packages"
sudo apt-get update -qq
sudo apt-get install -y -qq docker.io docker-compose-v2 git jq rclone ufw ca-certificates curl
sudo usermod -aG docker "$USER"
sudo timedatectl set-timezone Asia/Riyadh
if ! docker info >/dev/null 2>&1; then
  echo "Added $USER to the docker group. Log out and back in (or: newgrp docker), then re-run." >&2
  exit 0
fi
docker compose version

step "firewall: SSH only (the app is reached through Cloudflare Tunnel)"
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw --force enable

step "uv (Python toolchain for host jobs)"
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# shellcheck disable=SC1091
source "$REPO/bin/_env.sh"

step "secrets"
[ -f .env ] || bin/prod-secrets.sh
set -a; . ./.env; set +a
mkdir -p var && chmod 700 var

step "images"
docker compose build

step "database"
docker compose up -d --wait postgres redis
if [ -n "$BACKUP" ]; then
  ( cd "$BACKUP" && sha256sum -c --quiet SHA256SUMS )
  tables="$(docker exec thaqip-postgres-1 psql -U thaqip -d thaqip -Atq -c \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")"
  if [ "$tables" = "0" ]; then
    # Roles first (superset_ro etc.), so the dump's GRANTs apply. The superuser
    # line is stripped: it carries the OLD host's password and would silently
    # replace this server's POSTGRES_PASSWORD (found in the staging rehearsal).
    # Passwords of the other roles are reset by bootstrap-superset.sh.
    grep -vE "ROLE thaqip( |;)" "$BACKUP/globals.sql" \
      | docker exec -i thaqip-postgres-1 psql -U thaqip -d postgres -q 2>&1 \
      | grep -v "already exists" || true
    docker exec -i thaqip-postgres-1 pg_restore -U thaqip -d thaqip --no-owner --exit-on-error \
      < "$BACKUP/thaqip.dump"
    docker exec -i thaqip-postgres-1 psql -U thaqip -d thaqip -q -v ON_ERROR_STOP=1 < db/post-restore.sql
    echo "restored $BACKUP/thaqip.dump"
  else
    echo "database not empty ($tables tables); skipping restore"
  fi
fi
bin/migrate.sh

step "console credentials (new admin password + service token for this server)"
if [ ! -f var/credentials.env ]; then
  bin/bootstrap-auth.sh
else
  echo "var/credentials.env exists; keeping it"
fi

step "host scrapers need a browser"
( cd services/ingestion && "$UV" sync --extra db --extra browser -q \
  && "$UV" run --extra browser playwright install --with-deps chromium )

step "start stack"
# Superset cannot start before bootstrap-superset.sh writes its secrets, so
# bring everything else up (and healthy) first.
mapfile -t CORE < <(docker compose config --services | grep -vx superset)
docker compose up -d --remove-orphans --wait --wait-timeout 300 "${CORE[@]}"
bin/bootstrap-superset.sh
docker compose up -d --wait --wait-timeout 300

step "boot service + cron + first backup and drill"
sed -e "s#__REPO__#$REPO#g" -e "s#__USER__#$USER#g" deploy/systemd/thaqip.service \
  | sudo tee /etc/systemd/system/thaqip.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable thaqip.service
( crontab -l 2>/dev/null | grep -v "$REPO/bin/" || true; sed "s#__REPO__#$REPO#g" deploy/cron/thaqip.crontab ) | crontab -
bin/backup.sh
bin/restore-drill.sh

step "done"
docker compose ps --format '{{.Service}}\t{{.Status}}'
echo "Admin login and service token: $REPO/var/credentials.env"
echo "Next: Cloudflare Zero Trust -> tunnel public hostnames + Access policies (docs/DEPLOY.md)."
