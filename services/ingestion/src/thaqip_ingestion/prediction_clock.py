"""Prediction clock: record a blind market prediction for every open tender, daily.

Accuracy Stage 1. A price model's accuracy can only be proven against
predictions written down before the answer was knowable. This worker makes that
happen for every open Etimad tender instead of only the ones someone happened
to open in the console:

1. SNAPSHOT: for each Etimad tender that has no award and whose offers have not
   opened yet, compute the market prediction with the same engine the console
   serves (``p2w.orchestrator.tender_intelligence``) and store it once per day
   as ``origin='daily_snapshot'``. Suppressed predictions are stored too: "we
   refused to guess" is part of the record.
2. SCORE: copy newly scorable rows from ``prediction_scorecard`` (migration
   0019, where the blindness rule lives) into ``prediction_feedback``, so the
   readiness report and dashboards read one table.

Forsah tenders are skipped: we harvest no Forsah awards, so they could never
be scored.

Run:
  DATABASE_URL=... python -m thaqip_ingestion.prediction_clock [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import asyncpg

from . import db
from .p2w import orchestrator

log = logging.getLogger("thaqip.prediction_clock")

CONNECTOR = "pricing.clock"

#: The target from the accuracy plan: ~150 scored outcomes measure an interval
#: hit rate to within about +/-5 points at 95% confidence.
FIRST_REPORTABLE_SAMPLE = 150

_BLIND_OPEN_TENDERS_SQL = """
SELECT t.id
FROM tenders t
WHERE t.source = 'etimad'
  AND NOT EXISTS (SELECT 1 FROM awards a WHERE a.tender_id = t.id)
  AND NOT EXISTS (SELECT 1 FROM tender_award_first_seen f WHERE f.tender_id = t.id)
  AND coalesce(t.offers_opening_date, t.last_offer_date) > now()
  AND NOT EXISTS (
        SELECT 1 FROM price_predictions p
        WHERE p.tender_id = t.id AND p.origin = 'daily_snapshot'
          AND p.prediction_scope = 'MARKET' AND p.snapshot_date = $1)
ORDER BY coalesce(t.offers_opening_date, t.last_offer_date) ASC, t.id
LIMIT $2
"""

_SCORE_SQL = """
INSERT INTO prediction_feedback
    (prediction_id, actual_award_value, actual_winner_vendor_id, actual_bidder_count,
     interval_hit, abs_pct_error, user_feedback)
SELECT s.prediction_id, s.award_value,
       (SELECT a.vendor_id FROM awards a WHERE a.tender_id = s.tender_id ORDER BY a.id LIMIT 1),
       (SELECT count(*) FROM offers o WHERE o.tender_id = s.tender_id),
       s.interval_hit, s.abs_pct_error, NULL
FROM prediction_scorecard s
WHERE s.scorable
  AND NOT EXISTS (SELECT 1 FROM prediction_feedback f
                  WHERE f.prediction_id = s.prediction_id AND f.user_feedback IS NULL)
RETURNING prediction_id
"""

_STATUS_SQL = """
SELECT
  (SELECT count(DISTINCT tender_id) FROM price_predictions
    WHERE origin = 'daily_snapshot' AND prediction_scope = 'MARKET')          AS tenders_tracked,
  (SELECT count(*) FROM price_predictions
    WHERE origin = 'daily_snapshot' AND prediction_scope = 'MARKET')          AS snapshots,
  (SELECT count(*) FROM prediction_scorecard)                                  AS blind_awarded,
  (SELECT count(*) FROM prediction_scorecard WHERE scorable)                   AS scored,
  (SELECT count(*) FROM prediction_scorecard WHERE suppressed)                 AS suppressed,
  (SELECT round(100.0 * avg(interval_hit::int), 1) FROM prediction_scorecard
    WHERE scorable)                                                            AS interval_hit_pct
"""


@dataclass
class ClockRun:
    candidates: int = 0
    snapshotted: int = 0
    suppressed: int = 0
    failed: int = 0
    scored: int = 0


async def snapshot_tender(conn: Any, tender_id: int, today: date) -> tuple[bool, bool]:
    """Compute and store today's market snapshot. Returns (stored, suppressed)."""
    intel = await orchestrator.tender_intelligence(
        conn, tender_id=tender_id, include_competitors=False)
    prediction = orchestrator._prediction_from_dict(intel["market"])
    pid = await orchestrator.persist_prediction(
        conn, prediction, origin="daily_snapshot", snapshot_date=today)
    return pid is not None, prediction.suppression_reason is not None


async def run_once(pool: asyncpg.Pool, *, limit: int = 5000) -> ClockRun:
    today = datetime.now(UTC).date()
    run = ClockRun()
    run_id = await pool.fetchval(
        "INSERT INTO ingest_runs (connector) VALUES ($1) RETURNING id", CONNECTOR)
    error: str | None = None
    try:
        ids = [r["id"] for r in await pool.fetch(_BLIND_OPEN_TENDERS_SQL, today, limit)]
        run.candidates = len(ids)
        for tender_id in ids:
            try:
                async with pool.acquire() as conn:
                    stored, suppressed = await snapshot_tender(conn, tender_id, today)
            except Exception as exc:  # one tender must not stop the clock
                run.failed += 1
                log.warning("snapshot failed for tender %s: %s", tender_id, exc)
                continue
            if stored:
                run.snapshotted += 1
                run.suppressed += int(suppressed)
            else:
                run.failed += 1
        run.scored = len(await pool.fetch(_SCORE_SQL))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        await pool.execute(
            """UPDATE ingest_runs SET finished_at = now(), ok = $2, items_seen = $3,
                      items_new = $4, items_changed = $5, error = $6
               WHERE id = $1""",
            run_id, error is None and run.failed == 0, run.candidates, run.snapshotted,
            run.scored, error or (f"{run.failed} tender snapshots failed" if run.failed else None))
    return run


async def status(pool: asyncpg.Pool) -> dict[str, Any]:
    row = dict(await pool.fetchrow(_STATUS_SQL))
    row["first_reportable_sample"] = FIRST_REPORTABLE_SAMPLE
    if (row["scored"] or 0) < FIRST_REPORTABLE_SAMPLE:
        row["interval_hit_pct"] = None   # an anecdote, not an accuracy figure
    return row


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    try:
        started = time.monotonic()
        run = await run_once(pool, limit=args.limit)
        log.info("clock run: candidates=%d snapshotted=%d (suppressed=%d) failed=%d scored=%d in %.0fs",
                 run.candidates, run.snapshotted, run.suppressed, run.failed, run.scored,
                 time.monotonic() - started)
        log.info("clock status: %s", await status(pool))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
