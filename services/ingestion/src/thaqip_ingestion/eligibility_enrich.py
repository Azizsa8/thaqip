"""Fetch and store Etimad tender-detail fields for eligibility/fit scoring
(PRD "Thaqip for Contractors" §5.1, T-ELIG-01).

`award_watch.py` already calls `DetailsFetcher` for *pursued* tenders only,
to refine a pursuit's compliance checklist. This module calls the same
fetcher for any open tender that has none yet, and stores the result as a
structured `tender_details` row (db/migrations/0023_eligibility.sql)
instead of ad-hoc `compliance_items` text — that structured row is what
`thaqip_ingestion.fit_score` reads.

A failed fetch is stored too (`fetch_error` set, every other column null)
rather than leaving the tender silently unenriched with no record of why —
otherwise a permanently-broken tender would be retried forever, indistinguishable
from one that simply hasn't been picked up yet.

Run (cron, e.g. every 2h):
    DATABASE_URL=... uv run --extra db --extra browser \
        python -m thaqip_ingestion.eligibility_enrich
"""
from __future__ import annotations

import asyncio
import logging
import os

import asyncpg

from . import db
from .etimad.details import DetailsFetcher
from .etimad.session import PlaywrightSessionProvider
from .tender_details import parse_detail_fields

log = logging.getLogger("thaqip.eligibility_enrich")

# Only tenders still open for offers are worth spending a fetch on; a
# tender past its deadline no longer needs a fit score.
FIND_SQL = """
SELECT t.id AS tender_id, t.source_id_string
FROM tenders t
WHERE t.source = 'etimad' AND t.source_id_string IS NOT NULL
  AND (t.last_offer_date IS NULL OR t.last_offer_date > now())
  AND NOT EXISTS (SELECT 1 FROM tender_details d WHERE d.tender_id = t.id)
ORDER BY t.detected_at DESC
LIMIT $1
"""

UPSERT_SQL = """
INSERT INTO tender_details (
    tender_id, classification_required, classification_text, execution_location,
    activity_name_detail, enquiries_deadline, stop_period_days, expected_award_date,
    work_start_date, site_visit_date, relations_raw, dates_raw, fetch_error, fetched_at
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13, now())
ON CONFLICT (tender_id) DO UPDATE SET
    classification_required = EXCLUDED.classification_required,
    classification_text     = EXCLUDED.classification_text,
    execution_location       = EXCLUDED.execution_location,
    activity_name_detail     = EXCLUDED.activity_name_detail,
    enquiries_deadline       = EXCLUDED.enquiries_deadline,
    stop_period_days         = EXCLUDED.stop_period_days,
    expected_award_date      = EXCLUDED.expected_award_date,
    work_start_date          = EXCLUDED.work_start_date,
    site_visit_date          = EXCLUDED.site_visit_date,
    relations_raw            = EXCLUDED.relations_raw,
    dates_raw                = EXCLUDED.dates_raw,
    fetch_error              = EXCLUDED.fetch_error,
    fetched_at               = now()
"""


async def enrich_batch(pool: asyncpg.Pool, session, *, limit: int = 40, delay: float = 2.0) -> int:
    """Fetch+store tender_details for up to `limit` tenders. Returns the
    count successfully fetched (fetch failures are stored but not counted)."""
    rows = await pool.fetch(FIND_SQL, limit)
    if not rows:
        return 0
    fetcher = DetailsFetcher(session)
    fetched = 0
    try:
        for r in rows:
            try:
                raw = await fetcher.fetch(r["source_id_string"])
            except Exception as exc:  # noqa: BLE001 — record the failure, keep going
                log.warning("detail fetch failed for tender %s: %r", r["tender_id"], exc)
                await pool.execute(
                    UPSERT_SQL, r["tender_id"], None, None, None, None, None, None,
                    None, None, None, "{}", "{}", repr(exc),
                )
                continue
            parsed = parse_detail_fields(raw.relations, raw.dates)
            await pool.execute(
                UPSERT_SQL,
                r["tender_id"],
                parsed.classification_required,
                parsed.classification_text,
                parsed.execution_location,
                parsed.activity_name_detail,
                parsed.enquiries_deadline,
                parsed.stop_period_days,
                parsed.expected_award_date,
                parsed.work_start_date,
                parsed.site_visit_date,
                _to_jsonb(parsed.relations_raw),
                _to_jsonb(parsed.dates_raw),
                None,
            )
            fetched += 1
            await asyncio.sleep(delay)
    finally:
        await fetcher.aclose()
    return fetched


def _to_jsonb(d: dict[str, str]) -> str:
    import json
    return json.dumps(d, ensure_ascii=False)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    session = PlaywrightSessionProvider()
    try:
        fetched = await enrich_batch(pool, session)
        log.info("fetched tender_details for %d tenders", fetched)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
