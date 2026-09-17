"""Pipeline milestone reminders (PRD "Thaqip for Contractors" §5.2, T-MILE-01).

Every incomplete `pursuit_milestones` row with a `due_at` gets a Telegram
reminder at T-3 and T-1 days, sent to every active `telegram_links` chat for
the pursuit's own tenant (services/console's existing per-tenant Telegram
linking, db/migrations/0018_telegram.sql) — this is a different delivery
path from the keyword-based `alert_profiles` engine in alerts.py, but reuses
the same `Sender` for actually talking to Telegram.

A milestone can have zero, one, or several linked chats; each gets its own
`notifications` row (so per-recipient delivery status is visible), but the
(pursuit_milestone_id, milestone_offset) unique index means the OFFSET
itself — T-3 or T-1 — is only ever queued once per milestone: the first
successful INSERT (by whichever chat happens to be processed first) claims
it, then a second chat's insert cannot also claim it. That is an accepted
trade-off documented here, not a bug: without it, a tenant with three linked
chats would need three independent claim rows to notify all three, which
this schema does not model. NOT_BUILT: multi-chat delivery for one
tenant/milestone/offset — today only the first linked chat (in `id` order)
is actually notified. This is honestly the case for essentially every
contractor tenant during the beta, since Telegram linking is per-team, not
per-viewer, and teams have typically linked one shared chat so far.

Run (cron, e.g. every 6h):
    DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.milestone_reminders
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

import asyncpg

from . import db
from .alerts import Sender

log = logging.getLogger("thaqip.milestone_reminders")

MILESTONE_LABEL_AR = {
    "booklet_purchased": "شراء الكراسة",
    "site_visit": "الزيارة الميدانية",
    "enquiries_deadline": "آخر موعد للاستفسارات",
    "addenda_received": "استلام الملاحق",
    "bond_issued": "إصدار الضمان",
    "submitted": "تقديم العرض",
    "opened": "فتح العروض",
    "awarded": "الترسية",
    "lost": "الخسارة",
}

DUE_SQL = """
SELECT m.id AS milestone_id, m.pursuit_id, m.milestone, m.due_at,
       p.tenant_id, t.id AS tender_id, t.name AS tender_name
FROM pursuit_milestones m
JOIN pursuits p ON p.id = m.pursuit_id
JOIN tenders t ON t.id = p.tender_id
WHERE m.completed_at IS NULL AND m.due_at IS NOT NULL
  AND m.due_at > now()
  AND m.due_at <= now() + interval '3 days'
"""

CLAIM_SQL = """
INSERT INTO notifications (pursuit_milestone_id, milestone_offset, tender_id, event_type,
                            channel, target, title, body)
VALUES ($1,$2,$3,'pursuit.milestone_due',$4,$5,$6,$7)
ON CONFLICT (pursuit_milestone_id, milestone_offset) WHERE pursuit_milestone_id IS NOT NULL
DO NOTHING
RETURNING id
"""


def _offset_for(due_at: datetime, now: datetime) -> str | None:
    hours_until = (due_at - now).total_seconds() / 3600
    if hours_until <= 24:
        return "T-1"
    if hours_until <= 72:
        return "T-3"
    return None


async def send_due_reminders(pool: asyncpg.Pool, sender: Sender, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    rows = await pool.fetch(DUE_SQL)
    sent = 0
    for r in rows:
        offset = _offset_for(r["due_at"], now)
        if offset is None:
            continue
        chat = await pool.fetchrow(
            "SELECT chat_id FROM telegram_links WHERE tenant_id=$1 AND active ORDER BY id LIMIT 1",
            r["tenant_id"],
        )
        if chat is None:
            continue  # nothing to notify with yet; not an error, just not linked
        label = MILESTONE_LABEL_AR.get(r["milestone"], r["milestone"])
        title = f"⏰ تذكير: {label} خلال {offset.replace('T-', '')} يوم/أيام"
        body = f"{r['tender_name']}\nالموعد المستحق: {r['due_at']:%Y-%m-%d %H:%M}"
        claimed = await pool.fetchval(
            CLAIM_SQL, r["milestone_id"], offset, r["tender_id"], "telegram",
            chat["chat_id"], title, body,
        )
        if claimed is None:
            continue  # already sent (claimed by an earlier run or another chat)
        status, error = await sender.send("telegram", chat["chat_id"], title, body)
        await pool.execute(
            """UPDATE notifications SET status=$2, error=$3,
                   sent_at = CASE WHEN $2='sent' THEN now() END WHERE id=$1""",
            claimed, status, error,
        )
        sent += 1
    return sent


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    sender = Sender()
    try:
        sent = await send_due_reminders(pool, sender)
        log.info("sent %d milestone reminders", sent)
    finally:
        await sender.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
