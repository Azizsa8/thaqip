"""Telegram linking: one-time codes, single use, expiry, and tenant binding.

Runs against the real dev database; the Bot API is replaced by a recorder so
no network call is made and no token is needed.
"""
from __future__ import annotations

import asyncio
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

from thaqip_console.telegram import BAD_CODE_AR, WELCOME_AR, TelegramBot, _hash  # noqa: E402

DSN = os.environ.get("DATABASE_URL", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")
CHAT = "-990000000123"   # negative ids are groups; never a real user's private chat


class RecordingBot(TelegramBot):
    def __init__(self):
        super().__init__(token="000:test")
        self.username = "thaqip_test_bot"
        self.sent: list[tuple[str, str]] = []

    async def call(self, method, **payload):
        if method == "sendMessage":
            self.sent.append((payload["chat_id"], payload["text"]))
            return {}
        raise AssertionError(f"unexpected Bot API call {method}")


def _run(coro):
    return asyncio.run(coro)


async def _with_pool(fn):
    try:
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"database unavailable: {exc}")
    try:
        tid = await pool.fetchval("SELECT id FROM tenants WHERE slug='default'")
        await pool.execute("DELETE FROM telegram_links WHERE chat_id=$1", CHAT)
        return await fn(pool, tid)
    finally:
        await pool.execute("DELETE FROM telegram_links WHERE chat_id=$1", CHAT)
        await pool.close()


def _start(code: str) -> dict:
    return {"chat": {"id": int(CHAT), "type": "group", "title": "battery"},
            "from": {"username": "probe", "first_name": "Probe"},
            "text": f"/start {code}"}


def test_a_link_code_binds_the_chat_to_the_requesting_tenant_once():
    async def body(pool, tid):
        bot = RecordingBot()
        link = await bot.new_link(pool, tid, None)
        assert link["url"].startswith("https://t.me/thaqip_test_bot?start=")
        code = link["url"].split("start=", 1)[1]
        # only the hash is stored
        assert await pool.fetchval(
            "SELECT count(*) FROM telegram_link_codes WHERE code_hash=$1", code) == 0
        assert await pool.fetchval(
            "SELECT count(*) FROM telegram_link_codes WHERE code_hash=$1", _hash(code)) == 1

        await bot._handle(pool, _start(code))
        row = await pool.fetchrow(
            "SELECT tenant_id, active, chat_type FROM telegram_links WHERE chat_id=$1", CHAT)
        assert row["tenant_id"] == tid and row["active"] and row["chat_type"] == "group"
        assert bot.sent[-1] == (CHAT, WELCOME_AR)

        # replaying the same update (e.g. after a restart) must not re-link
        await pool.execute("UPDATE telegram_links SET active=false WHERE chat_id=$1", CHAT)
        await bot._handle(pool, _start(code))
        assert bot.sent[-1] == (CHAT, BAD_CODE_AR)
        assert not await pool.fetchval(
            "SELECT active FROM telegram_links WHERE chat_id=$1", CHAT)
    _run(_with_pool(body))


def test_expired_unknown_and_missing_codes_are_refused():
    async def body(pool, tid):
        bot = RecordingBot()
        link = await bot.new_link(pool, tid, None)
        code = link["url"].split("start=", 1)[1]
        await pool.execute(
            "UPDATE telegram_link_codes SET expires_at = now() - interval '1 second' "
            "WHERE code_hash=$1", _hash(code))
        for text in (f"/start {code}", "/start not-a-real-code", "/start"):
            msg = _start("x") | {"text": text}
            await bot._handle(pool, msg)
            assert bot.sent[-1] == (CHAT, BAD_CODE_AR), text
        assert await pool.fetchval(
            "SELECT count(*) FROM telegram_links WHERE chat_id=$1", CHAT) == 0
    _run(_with_pool(body))


def test_the_token_never_appears_in_a_reported_poll_error():
    """httpx errors can carry the request URL, and the URL carries the token."""
    token = "123456:SECRET-token-value-that-must-not-leak-xx"

    class LeakyBot(TelegramBot):
        async def call(self, method, **payload):
            raise RuntimeError(f"connect failed: https://api.telegram.org/bot{self.token}/{method}")

    async def body():
        bot = LeakyBot(token=token)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bot.poll_forever(pool=None), timeout=0.3)
        await bot.close()
        return bot.last_error

    err = _run(body())
    assert err and "***" in err
    assert token not in err and "SECRET" not in err


def test_linking_repoints_placeholder_telegram_profiles_only():
    async def body(pool, tid):
        bot = RecordingBot()
        placeholder = await pool.fetchval(
            """INSERT INTO alert_profiles (name, channel, target, tenant_id)
               VALUES ('tg-probe-placeholder', 'telegram', 'dev-console', $1) RETURNING id""", tid)
        explicit = await pool.fetchval(
            """INSERT INTO alert_profiles (name, channel, target, tenant_id)
               VALUES ('tg-probe-explicit', 'telegram', '12345', $1) RETURNING id""", tid)
        try:
            link = await bot.new_link(pool, tid, None)
            await bot._handle(pool, _start(link["url"].split("start=", 1)[1]))
            assert await pool.fetchval(
                "SELECT target FROM alert_profiles WHERE id=$1", placeholder) == CHAT
            assert await pool.fetchval(
                "SELECT target FROM alert_profiles WHERE id=$1", explicit) == "12345"
        finally:
            await pool.execute("DELETE FROM alert_profiles WHERE id = ANY($1)",
                               [placeholder, explicit])
    _run(_with_pool(body))
