#!/usr/bin/env bash
# Store the Telegram bot token for alerts, without it ever appearing on the
# command line, in shell history, or in git.
#
#   1. In Telegram, open @BotFather, send /newbot, pick a name and a username
#      ending in "bot". BotFather replies with a token like 123456:ABC-...
#   2. Run this script and paste the token when asked (input is hidden).
#   3. In the console: التنبيهات ← «ربط تيليجرام» ← press Start in Telegram.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/var/telegram.env"
mkdir -p "$REPO/var"

read -r -s -p "Paste the bot token from @BotFather: " TOKEN; echo
[[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || { echo "That does not look like a bot token." >&2; exit 1; }

# Validate before saving. Telegram takes the token in the URL path, so curl
# errors are silenced to keep the URL (and token) out of the terminal.
ME="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${TOKEN}/getMe" 2>/dev/null || true)"
case "$ME" in
  *'"ok":true'*) BOT="$(printf '%s' "$ME" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')";;
  *) echo "Telegram rejected the token (or is unreachable). Nothing was saved." >&2; exit 1;;
esac

umask 077
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN" > "$OUT"
chmod 600 "$OUT"
echo "Saved for @${BOT} -> $OUT"

cd "$REPO"
docker compose up -d --force-recreate console alerts >/dev/null
echo "console and alerts restarted with the token. Now link your chat from the console."
