#!/usr/bin/env bash
# First-run setup for Thaqip Analytics (Superset). Safe to re-run: secrets are
# generated once, roles and databases are created only if missing, and the
# content provisioner updates charts/dashboards in place.
#
#   var/superset.env     -> container env: SECRET_KEY + metadata DB URI (600)
#   var/credentials.env  -> appended: Superset admin login + read-only DB password
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_env.sh"
ENVF="$REPO/var/superset.env"
CRED="$REPO/var/credentials.env"
PSQL=(docker exec -i thaqip-postgres-1 psql -U thaqip -d thaqip -v ON_ERROR_STOP=1 -q)
cd "$REPO"
umask 077
mkdir -p var

rand() { head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }

if [ ! -f "$ENVF" ]; then
  META_PASS="$(rand 32)"
  {
    echo "SUPERSET_SECRET_KEY=$(rand 64)"
    echo "SUPERSET_METADATA_URI=postgresql+psycopg2://superset_meta:${META_PASS}@postgres:5432/superset"
  } > "$ENVF"
  chmod 600 "$ENVF"
  echo "generated $ENVF"
fi
META_PASS="$(sed -n 's#^SUPERSET_METADATA_URI=postgresql+psycopg2://superset_meta:\([^@]*\)@.*#\1#p' "$ENVF")"

touch "$CRED"; chmod 600 "$CRED"
if ! grep -q '^SUPERSET_ADMIN_PASSWORD=' "$CRED"; then
  {
    echo "SUPERSET_URL=http://127.0.0.1:8092"
    echo "SUPERSET_ADMIN_USER=admin"
    echo "SUPERSET_ADMIN_PASSWORD=$(rand 20)"
    echo "SUPERSET_RO_PASSWORD=$(rand 32)"
  } >> "$CRED"
  echo "added Superset credentials to $CRED"
fi
# shellcheck disable=SC1090
set -a; . "$CRED"; set +a

echo "== database roles"
bin/migrate.sh >/dev/null
"${PSQL[@]}" <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'superset_meta') THEN
    CREATE ROLE superset_meta LOGIN PASSWORD '${META_PASS}';
  ELSE
    ALTER ROLE superset_meta LOGIN PASSWORD '${META_PASS}';
  END IF;
END \$\$;
ALTER ROLE superset_ro LOGIN PASSWORD '${SUPERSET_RO_PASSWORD}';
-- superset_meta owns only its own database; it gets nothing in thaqip.
REVOKE ALL ON DATABASE thaqip FROM superset_meta;
SQL
if ! "${PSQL[@]}" -At -c "SELECT 1 FROM pg_database WHERE datname='superset'" | grep -q 1; then
  "${PSQL[@]}" -c "CREATE DATABASE superset OWNER superset_meta"
fi

echo "== image + metadata schema"
docker compose build -q superset
docker compose run --rm -T superset superset db upgrade >/dev/null 2>&1
docker compose run --rm -T -e SS_ADMIN_USER="$SUPERSET_ADMIN_USER" -e SS_ADMIN_PASS="$SUPERSET_ADMIN_PASSWORD" \
  superset sh -c 'superset fab create-admin --username "$SS_ADMIN_USER" --firstname Thaqip \
    --lastname Admin --email admin@thaqip.local --password "$SS_ADMIN_PASS"' 2>&1 | grep -iE "created|exists" || true
docker compose run --rm -T superset superset init >/dev/null 2>&1

echo "== start"
docker compose up -d superset
for _ in $(seq 1 90); do
  curl -sf -o /dev/null "$SUPERSET_URL/health" && break
  sleep 2
done
curl -sf -o /dev/null "$SUPERSET_URL/health" || { echo "superset did not become healthy" >&2; exit 1; }

echo "== content (datasets, charts, dashboards)"
cd "$REPO/services/ingestion"
"$UV" run python "$REPO/analytics/superset/provision.py"
echo "done: $SUPERSET_URL  (login: $SUPERSET_ADMIN_USER, password in var/credentials.env)"
