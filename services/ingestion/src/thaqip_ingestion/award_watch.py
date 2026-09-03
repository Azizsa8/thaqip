"""Award watcher for pursued tenders (closes the M5-1 loop automatically).

Every run: Etimad tenders that are in the War Room, past their deadline, and
without stored awards get their awarding component checked directly (instead
of waiting for the slow awarded-listing walk). When results are announced:
offers/awards stored, outcome award_value back-filled if the team already
logged a result, and the alert engine broadcasts it (pursued awards always
broadcast — see alerts.handle).

Run (cron every 4h):  DATABASE_URL=... uv run --extra db --extra browser \
    python -m thaqip_ingestion.award_watch
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import db
from .awards_harvest import AwardsHarvester, store_awarding
from .etimad.session import PlaywrightSessionProvider

log = logging.getLogger("thaqip.award_watch")

FIND_SQL = """
SELECT p.id AS pursuit_id, t.id AS tender_id, t.source_id_string, left(t.name,60) AS name
FROM pursuits p JOIN tenders t ON t.id = p.tender_id
WHERE t.source = 'etimad' AND t.source_id_string IS NOT NULL
  AND t.last_offer_date < now()
  AND NOT EXISTS (SELECT 1 FROM awards w WHERE w.tender_id = t.id)
LIMIT 40
"""


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    rows = await pool.fetch(FIND_SQL)
    if not rows:
        log.info("no pursued tenders awaiting award results")
        await pool.close()
        return
    harvester = AwardsHarvester(pool, PlaywrightSessionProvider())
    announced = 0
    try:
        for r in rows:
            result = await harvester.fetch_awarding(r["source_id_string"])
            if result.announced:
                await store_awarding(pool, r["tender_id"], result)
                await pool.execute(
                    """UPDATE outcomes o SET award_value = w.award_value
                       FROM awards w
                       WHERE o.pursuit_id = $1 AND w.tender_id = $2
                         AND o.award_value IS NULL""",
                    r["pursuit_id"], r["tender_id"])
                announced += 1
                log.info("award announced for pursued tender: %s", r["name"])
            await asyncio.sleep(3)
    finally:
        await harvester.aclose()
        log.info("checked %d pursued tenders, %d newly announced", len(rows), announced)
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
