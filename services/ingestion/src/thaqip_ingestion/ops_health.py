"""Operational health maintenance for Thaqip ingestion runs.

Use this for one-shot cleanup from cron/Modal/manual ops. It does not kill
processes; it only marks old unfinished ``ingest_runs`` rows as stalled so the
health dashboard reflects reality when a process disappeared without closing
its run record.

Run:
  DATABASE_URL=... python -m thaqip_ingestion.ops_health --close-stalled
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

from . import db

log = logging.getLogger("thaqip.ops_health")

DEFAULT_STALL_MINUTES = {
    "etimad.awards_harvest": 90,
    "etimad.backfill.all": 180,
    "etimad.backfill.awarded": 180,
    "pricing.seed": 30,
}


async def close_stalled_runs(pool, *, older_than_minutes: int | None = None) -> int:
    rows = await pool.fetch(
        """SELECT id, connector, started_at,
                  extract(epoch FROM now() - started_at) / 60 AS age_minutes
           FROM ingest_runs
           WHERE finished_at IS NULL AND ok IS NULL
           ORDER BY started_at"""
    )
    closed = 0
    for r in rows:
        threshold = older_than_minutes or DEFAULT_STALL_MINUTES.get(r["connector"], 60)
        if float(r["age_minutes"] or 0) <= threshold:
            continue
        checkpoint = {
            "stalled": True,
            "age_minutes": round(float(r["age_minutes"] or 0), 1),
            "threshold_minutes": threshold,
            "reason": "unfinished run exceeded watchdog threshold",
        }
        await pool.execute(
            """UPDATE ingest_runs
               SET finished_at=now(), ok=false, error='stalled watchdog closed orphaned run',
                   checkpoint=$2::jsonb
               WHERE id=$1 AND finished_at IS NULL AND ok IS NULL""",
            r["id"],
            json.dumps(checkpoint),
        )
        closed += 1
        log.warning("closed stalled ingest run id=%s connector=%s", r["id"], r["connector"])
    return closed


async def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip ops health maintenance")
    parser.add_argument("--close-stalled", action="store_true", help="mark old unfinished ingest runs as stalled")
    parser.add_argument("--older-than-minutes", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not args.close_stalled:
        parser.error("nothing to do; pass --close-stalled")
    pool = await db.connect(os.environ["DATABASE_URL"])
    run_id = await pool.fetchval("INSERT INTO ingest_runs (connector) VALUES ('ops.health') RETURNING id")
    error: str | None = None
    closed = 0
    try:
        closed = await close_stalled_runs(pool, older_than_minutes=args.older_than_minutes)
        log.info("closed %d stalled ingest runs", closed)
    except Exception as exc:
        error = repr(exc)
        raise
    finally:
        await pool.execute(
            """UPDATE ingest_runs
               SET finished_at=now(), ok=$2, error=$3, items_changed=$4,
                   checkpoint=$5::jsonb
               WHERE id=$1""",
            run_id,
            error is None,
            error,
            closed,
            json.dumps({"closed_stalled_runs": closed, "older_than_minutes": args.older_than_minutes}),
        )
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
