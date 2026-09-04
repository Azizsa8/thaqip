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
from .etimad.details import DetailsFetcher
from .etimad.session import PlaywrightSessionProvider

log = logging.getLogger("thaqip.award_watch")

ENRICH_MARK = "تفاصيل اعتماد"

ENRICH_SQL = """
SELECT p.id AS pursuit_id, t.id AS tender_id, t.source_id_string
FROM pursuits p JOIN tenders t ON t.id = p.tender_id
WHERE t.source = 'etimad' AND t.source_id_string IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM compliance_items c
                   WHERE c.pursuit_id = p.id AND c.source_ref LIKE '%' || $1 || '%')
LIMIT 20
"""


async def enrich_pursuits(pool, session) -> int:
    """Refine Etimad pursuit matrices from the tender's detail components:
    classification requirement (required/not + field), execution location."""
    rows = await pool.fetch(ENRICH_SQL, ENRICH_MARK)
    if not rows:
        return 0
    fetcher = DetailsFetcher(session)
    enriched = 0
    try:
        for r in rows:
            try:
                detail = await fetcher.fetch(r["source_id_string"])
            except Exception as exc:  # noqa: BLE001 — skip a broken detail, keep going
                log.warning("detail fetch failed for tender %s: %r", r["tender_id"], exc)
                continue
            rel = detail.relations
            classification = next((v for k, v in rel.items() if "التصنيف" in k), None)
            location = next((v for k, v in rel.items() if "مكان التنفيذ" in k), None)
            async with pool.acquire() as conn, conn.transaction():
                if classification and "غير مطلوب" in classification:
                    await conn.execute(
                        """UPDATE compliance_items SET status='n_a',
                               source_ref = source_ref || ' · تفاصيل اعتماد: غير مطلوب'
                           WHERE pursuit_id=$1 AND requirement LIKE '%تصنيف المقاولين%'
                             AND status='missing'""", r["pursuit_id"])
                elif classification:
                    await conn.execute(
                        """INSERT INTO compliance_items
                             (pursuit_id, requirement, category, source_ref, origin, sort_order)
                           VALUES ($1, $2, 'qualification', 'تفاصيل اعتماد — مجال التصنيف', 'rule', 22)""",
                        r["pursuit_id"], f"تصنيف مطلوب: {classification[:120]}")
                if location:
                    await conn.execute(
                        """INSERT INTO compliance_items
                             (pursuit_id, requirement, category, source_ref, origin, sort_order)
                           VALUES ($1, $2, 'general', 'تفاصيل اعتماد — مكان التنفيذ', 'rule', 90)""",
                        r["pursuit_id"], f"مكان التنفيذ: {location[:120]}")
                # marker so the pursuit isn't re-enriched
                await conn.execute(
                    """INSERT INTO compliance_items
                         (pursuit_id, requirement, category, source_ref, origin, sort_order)
                       VALUES ($1, 'مواعيد المنافسة مؤكدة من صفحة التفاصيل', 'deadline',
                               'تفاصيل اعتماد — المواعيد', 'rule', 3)""", r["pursuit_id"])
                await conn.execute(
                    "UPDATE compliance_items SET status='met' WHERE pursuit_id=$1 AND requirement='مواعيد المنافسة مؤكدة من صفحة التفاصيل'",
                    r["pursuit_id"])
            enriched += 1
            await asyncio.sleep(2)
    finally:
        await fetcher.aclose()
    return enriched

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
    session = PlaywrightSessionProvider()
    rows = await pool.fetch(FIND_SQL)
    enriched = await enrich_pursuits(pool, session)
    if enriched:
        log.info("enriched %d pursuits from detail components", enriched)
    if not rows:
        log.info("no pursued tenders awaiting award results")
        await pool.close()
        return
    harvester = AwardsHarvester(pool, session)
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
