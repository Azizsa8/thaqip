"""Telegram linking for alert delivery.

The alerts worker already sends with the Bot API; what was missing is a way to
learn a user's chat id without asking them to find and paste it. The console
issues a one-time deep link, t.me/<bot>?start=<code>. Pressing Start sends
"/start <code>" to the bot, and the poller below records that chat against the
tenant (and user) that asked for the code.

Only the sha256 of a code is stored; it is single-use and expires in minutes,
so replayed updates after a restart are harmless.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx

log = logging.getLogger("thaqip.telegram")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
CODE_TTL = timedelta(minutes=10)
API = "https://api.telegram.org/bot{token}/{method}"

WELCOME_AR = ("✅ تم ربط هذه المحادثة بثاقب.\n"
              "ستصلك هنا تنبيهات المنافسات حسب ملفات التنبيه في وحدة التحكم.")
BAD_CODE_AR = ("رابط الربط غير صالح أو انتهت صلاحيته.\n"
               "افتح وحدة تحكم ثاقب ← التنبيهات ← «ربط تيليجرام» لإنشاء رابط جديد.")
HELP_AR = ("هذا بوت تنبيهات ثاقب. للربط افتح وحدة التحكم ← التنبيهات ← «ربط تيليجرام».")


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class TelegramBot:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.username: str | None = None
        self.last_error: str | None = None
        self.last_poll_at: datetime | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def call(self, method: str, **payload: Any) -> Any:
        r = await self._client.post(API.format(token=self.token, method=method), json=payload)
        body = r.json()
        if not body.get("ok"):
            # Never echo the URL: it contains the token.
            raise RuntimeError(f"telegram {method}: {body.get('error_code')} "
                               f"{body.get('description', '')[:160]}")
        return body["result"]

    async def ensure_identity(self) -> str | None:
        if self.username is None and self.configured:
            me = await self.call("getMe")
            self.username = me.get("username")
        return self.username

    async def send(self, chat_id: str, text: str) -> None:
        await self.call("sendMessage", chat_id=chat_id, text=text,
                        disable_web_page_preview=True)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- linking
    async def new_link(self, pool: asyncpg.Pool, tenant_id: int,
                       user_id: int | None) -> dict[str, Any]:
        username = await self.ensure_identity()
        code = secrets.token_urlsafe(18)          # [A-Za-z0-9_-], Telegram-safe
        expires = datetime.now(UTC) + CODE_TTL
        await pool.execute(
            """INSERT INTO telegram_link_codes (tenant_id, user_id, code_hash, expires_at)
               VALUES ($1, $2, $3, $4)""", tenant_id, user_id, _hash(code), expires)
        return {"url": f"https://t.me/{username}?start={code}",
                "bot_username": username, "expires_at": expires.isoformat()}

    async def _handle(self, pool: asyncpg.Pool, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        text = (message.get("text") or "").strip()
        chat_id = str(chat.get("id", ""))
        if not chat_id or not text.startswith("/start"):
            if chat.get("type") == "private" and chat_id:
                await self.send(chat_id, HELP_AR)
            return
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) == 2 else ""
        row = None
        if code:
            row = await pool.fetchrow(
                """UPDATE telegram_link_codes SET used_at = now()
                   WHERE code_hash = $1 AND used_at IS NULL AND expires_at > now()
                   RETURNING tenant_id, user_id""", _hash(code))
        if row is None:
            await self.send(chat_id, BAD_CODE_AR)
            return
        sender = message.get("from") or {}
        await pool.execute(
            """INSERT INTO telegram_links (tenant_id, user_id, chat_id, chat_type, chat_title,
                                          tg_username, first_name)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (tenant_id, chat_id) DO UPDATE
                 SET active = true, linked_at = now(), user_id = EXCLUDED.user_id,
                     chat_type = EXCLUDED.chat_type, chat_title = EXCLUDED.chat_title,
                     tg_username = EXCLUDED.tg_username, first_name = EXCLUDED.first_name""",
            row["tenant_id"], row["user_id"], chat_id, chat.get("type", "private"),
            chat.get("title"), sender.get("username"), sender.get("first_name"))
        # Telegram profiles created before any chat was linked hold a
        # placeholder target; point them at this chat so they start delivering.
        await pool.execute(
            """UPDATE alert_profiles SET target = $2
               WHERE tenant_id = $1 AND channel = 'telegram' AND target !~ '^-?[0-9]+$'""",
            row["tenant_id"], chat_id)
        await self.send(chat_id, WELCOME_AR)

    async def poll_forever(self, pool: asyncpg.Pool) -> None:
        """Long-poll getUpdates. One console process owns the bot's update
        stream; the alerts worker only calls sendMessage, which does not
        compete for updates."""
        offset: int | None = None
        backoff = 2.0
        while True:
            try:
                await self.ensure_identity()
                payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message"]}
                if offset is not None:
                    payload["offset"] = offset
                updates = await self.call("getUpdates", **payload)
                self.last_poll_at = datetime.now(UTC)
                self.last_error = None
                backoff = 2.0
                for update in updates:
                    offset = update["update_id"] + 1
                    if "message" in update:
                        try:
                            await self._handle(pool, update["message"])
                        except Exception as exc:  # one bad update must not stop the loop
                            log.warning("telegram update %s failed: %s", update["update_id"], exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc).replace(self.token, "***")[:200]
                log.warning("telegram poll failed: %s", self.last_error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)
