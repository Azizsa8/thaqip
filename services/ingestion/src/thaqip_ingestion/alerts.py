"""Phase 1 alert engine (PRD M2-3): event stream -> matched profiles -> delivery.

Consumes `thaqip.events` via a Redis consumer group ('alerts'), so it scales
horizontally and never loses an event (XACK only after the notification row is
durably written). Delivery is idempotent per (profile, event) via a unique key
— redelivered stream entries can't double-notify.

Channels:
  telegram — Bot API sendMessage (needs TELEGRAM_BOT_TOKEN; target = chat id)
  log      — writes the row and marks it sent (dev / testing channel)
  email    — row is written 'pending' for a future SMTP worker

Run:
  DATABASE_URL=... REDIS_URL=... [TELEGRAM_BOT_TOKEN=...] \
      uv run --extra db python -m thaqip_ingestion.alerts [--once]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

import asyncpg
import httpx
import redis.asyncio as aioredis

from . import db

log = logging.getLogger("thaqip.alerts")

STREAM, GROUP = "thaqip.events", "alerts"
BLOCK_MS, BATCH = 5000, 100

EVENT_LABEL = {
    "tender.created": "منافسة جديدة",
    "tender.extended": "تمديد موعد التقديم",
    "tender.updated": "تحديث على منافسة",
    "tender.awarded": "إعلان ترسية",
    "tender.deadline": "⏰ اقتراب موعد الإغلاق",
    "competition.rising": "📈 المنافسة تشتد على منافسة تتابعونها",
}

# Pursuit-deadline reminders concern the whole team: broadcast to every active
# profile regardless of its keyword/activity filters.
BROADCAST_EVENTS = {"tender.deadline", "competition.rising"}


def render(event_type: str, t: dict) -> tuple[str, str]:
    label = EVENT_LABEL.get(event_type, event_type)
    title = f"ثاقب · {label}"
    lines = [f"📌 {t['name']}"]
    if t.get("agency_name_raw"):
        lines.append(f"الجهة: {t['agency_name_raw']}")
    if t.get("activity_name_raw"):
        lines.append(f"النشاط: {t['activity_name_raw']}")
    if t.get("last_offer_date"):
        lines.append(f"آخر تقديم: {str(t['last_offer_date'])[:16]}")
    lines.append(f"المصدر: {'اعتماد' if t['source']=='etimad' else 'فرصة'} · مرجع {t.get('reference_number','')}")
    return title, "\n".join(lines)


def matches(profile: dict, t: dict, event_type: str) -> bool:
    if event_type not in profile["event_types"]:
        return False
    if t["source"] not in profile["sources"]:
        return False
    if profile["activity_ids"] and t.get("activity_id") not in profile["activity_ids"]:
        return False
    if profile["agency_ids"] and t.get("agency_id") not in profile["agency_ids"]:
        return False
    if profile["keywords"]:
        hay = f"{t.get('name','')} {t.get('activity_name_raw','')}"
        if not any(k in hay for k in profile["keywords"]):
            return False
    return True


class Sender:
    def __init__(self) -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self._http = httpx.AsyncClient(timeout=20)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, channel: str, target: str, title: str, body: str) -> tuple[str, str | None]:
        """Returns (status, error)."""
        if channel == "log":
            log.info("ALERT [%s] %s | %s", target, title, body.replace("\n", " ¶ "))
            return "sent", None
        if channel == "telegram":
            if not self.token:
                return "pending", "TELEGRAM_BOT_TOKEN not configured"
            r = await self._http.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": target, "text": f"{title}\n\n{body}"},
            )
            if r.status_code == 200:
                return "sent", None
            return "failed", f"telegram {r.status_code}: {r.text[:120]}"
        return "pending", f"channel {channel} has no worker yet"


async def handle(pool: asyncpg.Pool, sender: Sender, fields: dict) -> int:
    event_type = fields["event_type"]
    if fields.get("entity_type") != "tender" or event_type not in EVENT_LABEL:
        return 0
    try:
        data = json.loads(fields.get("data") or "{}")
    except (TypeError, ValueError):
        data = {}
    if isinstance(data, dict) and data.get("backfill"):
        return 0  # historical award found by the backfill lane: corpus, not news
    event_id, tender_id = int(fields["event_id"]), int(fields["entity_id"])
    t = await pool.fetchrow("SELECT * FROM tenders WHERE id=$1", tender_id)
    if t is None:
        return 0
    t = dict(t)
    profiles = await pool.fetch("SELECT * FROM alert_profiles WHERE active")
    # updates on tenders the team pursues or follows always broadcast
    broadcast = event_type in BROADCAST_EVENTS or (
        event_type in ("tender.awarded", "tender.updated", "tender.extended")
        and await pool.fetchval(
            """SELECT 1 WHERE EXISTS (SELECT 1 FROM pursuits WHERE tender_id=$1)
                       OR EXISTS (SELECT 1 FROM follows  WHERE tender_id=$1)""",
            tender_id)
    )
    delivered = 0
    for p in map(dict, profiles):
        if not broadcast and not matches(p, t, event_type):
            continue
        title, body = render(event_type, t)
        row = await pool.fetchrow(
            """INSERT INTO notifications (profile_id, event_id, tender_id, event_type,
                                          channel, target, title, body)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT (profile_id, event_id) DO NOTHING RETURNING id""",
            p["id"], event_id, tender_id, event_type, p["channel"], p["target"], title, body,
        )
        if row is None:  # already notified for this event (redelivery)
            continue
        digest_interval = p.get("digest_interval") or "instant"
        if digest_interval == "instant":
            status, error = await sender.send(p["channel"], p["target"], title, body)
        else:
            status, error = "pending", f"queued for {digest_interval} digest"
        await pool.execute(
            """UPDATE notifications SET status=$2, error=$3,
                   sent_at = CASE WHEN $2='sent' THEN now() END WHERE id=$1""",
            row["id"], status, error,
        )
        delivered += 1
    return delivered


async def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip alert engine")
    parser.add_argument("--once", action="store_true", help="drain backlog and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    pool = await db.connect(os.environ["DATABASE_URL"])
    r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6380/0"),
                          decode_responses=True)
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
    sender = Sender()
    log.info("alert engine consuming %s as group=%s", STREAM, GROUP)
    try:
        while True:
            try:
                resp = await r.xreadgroup(GROUP, "worker-1", {STREAM: ">"},
                                          count=BATCH, block=0 if args.once else BLOCK_MS)
            except (TimeoutError, aioredis.TimeoutError, aioredis.ConnectionError) as exc:
                log.debug("blocking read cycle: %r", exc)
                await asyncio.sleep(1)
                continue
            if not resp:
                if args.once:
                    break
                continue
            n_events = n_sent = 0
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    n_events += 1
                    try:
                        n_sent += await handle(pool, sender, fields)
                        await r.xack(STREAM, GROUP, entry_id)
                    except Exception:
                        log.exception("failed handling %s (left unacked for retry)", entry_id)
            if n_events:
                log.info("processed %d events -> %d notifications", n_events, n_sent)
            if args.once and n_events < BATCH:
                break
    finally:
        await sender.aclose()
        await r.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
