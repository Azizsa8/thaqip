"""Daily digest (PRD M2-9): one morning summary per active profile.

Summarizes the last 24h of notifications for each profile into a single
message on the profile's channel — so a busy profile gets one digest at 07:00
KSA instead of relying on having seen every instant alert.

Run (cron 07:00 KSA):  DATABASE_URL=... REDIS_URL=... uv run --extra db \
    python -m thaqip_ingestion.digest
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import db
from .alerts import EVENT_LABEL, Sender

log = logging.getLogger("thaqip.digest")

SUMMARY_SQL = """
SELECT n.event_type, count(*) AS n,
       array_agg(left(t.name, 60) ORDER BY n.id DESC) AS names
FROM notifications n JOIN tenders t ON t.id = n.tender_id
WHERE n.profile_id = $1 AND n.created_at > now() - interval '24 hours'
GROUP BY n.event_type ORDER BY n DESC
"""


def render_digest(profile_name: str, groups: list) -> tuple[str, str]:
    total = sum(g["n"] for g in groups)
    title = f"ثاقب · الملخص اليومي — {profile_name}"
    lines = [f"📬 {total} تنبيهًا خلال آخر 24 ساعة:\n"]
    for g in groups:
        label = EVENT_LABEL.get(g["event_type"], g["event_type"])
        lines.append(f"● {label}: {g['n']}")
        for name in g["names"][:5]:
            lines.append(f"   – {name}")
        if g["n"] > 5:
            lines.append(f"   … و{g['n'] - 5} أخرى")
    return title, "\n".join(lines)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    sender = Sender()
    sent = 0
    try:
        for p in await pool.fetch("SELECT * FROM alert_profiles WHERE active"):
            groups = await pool.fetch(SUMMARY_SQL, p["id"])
            if not groups:
                continue
            title, body = render_digest(p["name"], [dict(g) for g in groups])
            status, error = await sender.send(p["channel"], p["target"], title, body)
            await pool.execute(
                """INSERT INTO notifications (profile_id, event_id, tender_id, event_type,
                                              channel, target, title, body, status, error, sent_at)
                   VALUES ($1, NULL, NULL, 'digest.daily', $2, $3, $4, $5, $6, $7,
                           CASE WHEN $6='sent' THEN now() END)""",
                p["id"], p["channel"], p["target"], title, body, status, error,
            )
            sent += 1
        log.info("digests sent: %d", sent)
    finally:
        await sender.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
