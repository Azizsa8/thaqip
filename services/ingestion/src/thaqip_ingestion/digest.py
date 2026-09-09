"""Daily digest (PRD M2-9): one morning summary per active profile.

Summarizes the last 24h of notifications for each profile into a single
message on the profile's channel. Profiles choose instant, hourly, or daily
delivery; this worker sends the requested digest interval only.

Run:
  DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.digest --interval daily
  DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.digest --interval hourly
"""
from __future__ import annotations

import argparse
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
WHERE n.profile_id = $1
  AND n.created_at > now() - make_interval(secs => $2)
  AND n.event_type NOT LIKE 'digest.%'
  AND n.status = 'pending'
GROUP BY n.event_type ORDER BY n DESC
"""


def render_digest(profile_name: str, groups: list, interval: str) -> tuple[str, str]:
    total = sum(g["n"] for g in groups)
    interval_ar = "الساعة الماضية" if interval == "hourly" else "آخر 24 ساعة"
    title = f"ثاقب · ملخص {'ساعي' if interval == 'hourly' else 'يومي'} — {profile_name}"
    lines = [f"📬 {total} تنبيهًا خلال {interval_ar}:\n"]
    for g in groups:
        label = EVENT_LABEL.get(g["event_type"], g["event_type"])
        lines.append(f"● {label}: {g['n']}")
        for name in g["names"][:5]:
            lines.append(f"   – {name}")
        if g["n"] > 5:
            lines.append(f"   … و{g['n'] - 5} أخرى")
    return title, "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip alert digest sender")
    parser.add_argument("--interval", choices=("hourly", "daily"), default="daily")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    sender = Sender()
    sent = 0
    window_seconds = 3600 if args.interval == "hourly" else 24 * 3600
    event_type = f"digest.{args.interval}"
    try:
        for p in await pool.fetch("SELECT * FROM alert_profiles WHERE active AND digest_interval=$1", args.interval):
            groups = await pool.fetch(SUMMARY_SQL, p["id"], window_seconds)
            if not groups:
                continue
            title, body = render_digest(p["name"], [dict(g) for g in groups], args.interval)
            status, error = await sender.send(p["channel"], p["target"], title, body)
            await pool.execute(
                """INSERT INTO notifications (profile_id, event_id, tender_id, event_type,
                                              channel, target, title, body, status, error, sent_at)
                   VALUES ($1, NULL, NULL, $2, $3, $4, $5, $6, $7, $8,
                           CASE WHEN $7='sent' THEN now() END)""",
                p["id"], event_type, p["channel"], p["target"], title, body, status, error,
            )
            if status == "sent":
                await pool.execute(
                    """UPDATE notifications
                       SET status='sent', error=NULL, sent_at=coalesce(sent_at, now())
                       WHERE profile_id=$1
                         AND created_at > now() - make_interval(secs => $2)
                         AND event_type NOT LIKE 'digest.%'
                         AND status='pending'""",
                    p["id"], window_seconds,
                )
            sent += 1
        log.info("%s digests sent: %d", args.interval, sent)
    finally:
        await sender.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
