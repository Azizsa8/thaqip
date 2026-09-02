"""Freshness SLO measurement (ticket D3).

Detection latency = detected_at (when Thaqip first stored the tender) minus
published_at (the source's submitionDate). This powers the internal SLO
dashboard now and the public freshness board (M1-6) in Phase 1.

The number is only meaningful once the delta loop (D2) runs continuously —
tenders ingested by backfill sessions are excluded by the --window filter,
which looks at recently *published* tenders only.

Run:
  DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.freshness --window 24
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from . import db

log = logging.getLogger("thaqip.freshness")

SUMMARY_SQL = """
WITH recent AS (
  SELECT id, name, published_at, detected_at,
         detected_at - published_at AS latency
  FROM tenders
  WHERE source = 'etimad'
    AND published_at IS NOT NULL
    AND published_at > now() - ($1 || ' hours')::interval
    AND detected_at >= published_at
)
SELECT count(*)                                                   AS tenders,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency)       AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency)      AS p95,
       min(latency)                                               AS best,
       max(latency)                                               AS worst
FROM recent
"""

WORST_SQL = """
SELECT source_tender_id, left(name, 60) AS name, published_at,
       detected_at - published_at AS latency
FROM tenders
WHERE source = 'etimad' AND published_at IS NOT NULL
  AND published_at > now() - ($1 || ' hours')::interval
  AND detected_at >= published_at
ORDER BY latency DESC LIMIT $2
"""


async def report(window_hours: int, worst_n: int = 5) -> None:
    pool = await db.connect(os.environ["DATABASE_URL"])
    try:
        row = await pool.fetchrow(SUMMARY_SQL, str(window_hours))
        print(f"Freshness — tenders published in the last {window_hours}h")
        print(f"  tenders : {row['tenders']}")
        if row["tenders"]:
            print(f"  p50     : {row['p50']}")
            print(f"  p95     : {row['p95']}")
            print(f"  best    : {row['best']}")
            print(f"  worst   : {row['worst']}")
            print(f"  SLO     : p50 < 15 min -> {'PASS' if row['p50'].total_seconds() < 900 else 'PENDING (needs continuous D2 loop)'}")
            print("  slowest detections:")
            for w in await pool.fetch(WORST_SQL, str(window_hours), worst_n):
                print(f"    {w['source_tender_id']}  +{w['latency']}  {w['name']}")
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip freshness report")
    parser.add_argument("--window", type=int, default=24, help="hours of published tenders")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(report(args.window))


if __name__ == "__main__":
    main()
