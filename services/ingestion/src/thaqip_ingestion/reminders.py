"""Pursuit deadline reminders (PRD M3-6 lite).

Hourly cron: any active pursuit (stage not submitted/won/lost) whose tender
closes within a threshold gets ONE `tender.deadline` outbox event per
threshold (72h, 24h). The alert engine broadcasts these to all active
profiles — a pursuit deadline concerns the whole team, so profile filters
don't apply. Dedupe: the event carries a deterministic marker checked before
emitting; downstream, notifications are already unique per (profile, event).

Run (cron hourly):  DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.reminders
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from . import db

log = logging.getLogger("thaqip.reminders")

THRESHOLDS_H = (72, 24)

FIND_SQL = """
SELECT p.id AS pursuit_id, t.id AS tender_id, t.name, t.last_offer_date,
       extract(epoch FROM t.last_offer_date - now())/3600.0 AS hours_left
FROM pursuits p JOIN tenders t ON t.id = p.tender_id
WHERE p.stage NOT IN ('submitted','won','lost')
  AND t.last_offer_date > now()
  AND t.last_offer_date < now() + ($1 || ' hours')::interval
"""

ALREADY_SQL = """
SELECT 1 FROM ingest_events
WHERE event_type = 'tender.deadline' AND entity_type = 'tender' AND entity_id = $1
  AND data->>'threshold_h' = $2
"""


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    emitted = 0
    try:
        for threshold in THRESHOLDS_H:
            for row in await pool.fetch(FIND_SQL, str(threshold)):
                if await pool.fetchval(ALREADY_SQL, row["tender_id"], str(threshold)):
                    continue
                await pool.execute(
                    """INSERT INTO ingest_events (event_type, entity_type, entity_id, data)
                       VALUES ('tender.deadline', 'tender', $1, $2::jsonb)""",
                    row["tender_id"],
                    json.dumps({"threshold_h": str(threshold),
                                "hours_left": round(float(row["hours_left"]), 1),
                                "pursuit_id": row["pursuit_id"]}),
                )
                emitted += 1
        log.info("deadline reminders emitted: %d", emitted)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
