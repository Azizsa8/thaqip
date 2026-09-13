#!/usr/bin/env bash
# Creates the first admin user and the service token, writing both to
# var/credentials.env (gitignored). Safe to re-run: it will not overwrite an
# existing credentials file or duplicate the user.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_env.sh"
CRED="$REPO/var/credentials.env"
mkdir -p "$REPO/var"

if [ -f "$CRED" ]; then
  echo "credentials already exist at $CRED — not regenerating"
  exit 0
fi

ADMIN_USER="${THAQIP_ADMIN_USER:-admin}"
ADMIN_PASS="${THAQIP_ADMIN_PASSWORD:-$("$UV" run --project "$REPO/services/console" python -c 'import secrets;print(secrets.token_urlsafe(18))')}"
API_TOKEN="$("$UV" run --project "$REPO/services/console" python -c 'import secrets;print(secrets.token_urlsafe(32))')"

cd "$REPO/services/console"
"$UV" run python - "$ADMIN_USER" "$ADMIN_PASS" <<'PYEOF'
import asyncio, os, sys
sys.path.insert(0, "src")
import asyncpg
from thaqip_console.auth import hash_password

async def main(username, password):
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    tid = await conn.fetchval("SELECT id FROM tenants WHERE slug='default'")
    if tid is None:
        tid = await conn.fetchval(
            "INSERT INTO tenants (slug, name) VALUES ('default','Default') RETURNING id")
    existing = await conn.fetchval("SELECT id FROM users WHERE lower(username)=lower($1)", username)
    if existing:
        await conn.execute("UPDATE users SET password_hash=$2, active=true WHERE id=$1",
                           existing, hash_password(password))
        print(f"updated existing user {username}")
    else:
        await conn.execute(
            """INSERT INTO users (tenant_id, username, password_hash, role)
               VALUES ($1,$2,$3,'admin')""", tid, username, hash_password(password))
        print(f"created admin user {username}")
    await conn.close()

asyncio.run(main(sys.argv[1], sys.argv[2]))
PYEOF

umask 077
cat > "$CRED" <<EOF
# Thaqip console credentials — generated $(date -Is). DO NOT COMMIT.
THAQIP_ADMIN_USER=$ADMIN_USER
THAQIP_ADMIN_PASSWORD=$ADMIN_PASS
THAQIP_API_TOKEN=$API_TOKEN
EOF
chmod 600 "$CRED"
# The console container reads only the service token (never the admin password).
printf 'THAQIP_API_TOKEN=%s\n' "$API_TOKEN" > "$REPO/var/console.env"
chmod 600 "$REPO/var/console.env"
echo
echo "  console login : $ADMIN_USER / $ADMIN_PASS"
echo "  service token : $API_TOKEN"
echo "  saved to      : $CRED  (mode 600, gitignored)"
