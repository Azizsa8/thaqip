"""Nightly reconciliation (ticket C5).

Three checks, cheap by design (a handful of listing requests):

1. **Census** — source totalCount per category (all / awarded) vs what the
   corpus holds, so backfill progress and silent capture gaps are visible as
   one number per night.
2. **Head sample** — re-walk the newest N listing pages and upsert every row;
   any tender the delta loop somehow missed is healed on the spot and counted.
3. **Staleness sweep** — open tenders in the corpus whose last_offer_date
   passed more than grace-days ago get flagged in the report (detail refetch
   picks them up once B2's detail lane is scheduled).

The nightly report lands in ingest_runs.checkpoint (connector
'etimad.reconcile') and significant gaps emit a `reconcile.gap` outbox event
so alerting can pick it up later.

Run:  DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.reconcile
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from . import db
from .etimad.client import EtimadClient

log = logging.getLogger("thaqip.reconcile")

CATEGORIES = {"all": {}, "awarded": {"TenderCategory": 6}}
GAP_EVENT_THRESHOLD = 0.02  # >2% head-sample miss rate is worth an event


async def census(client: EtimadClient, pool) -> dict:
    out = {}
    for name, extra in CATEGORIES.items():
        listing = await client.fetch_listing_page(1, 1, extra_params=extra)
        source_total = listing.totalCount
        if name == "all":
            db_total = await pool.fetchval("SELECT count(*) FROM tenders WHERE source='etimad'")
        else:
            db_total = await pool.fetchval(
                "SELECT count(*) FROM tenders t WHERE source='etimad' AND EXISTS "
                "(SELECT 1 FROM awards a WHERE a.tender_id=t.id)"
            )
        out[name] = {
            "source_total": source_total,
            "db_total": db_total,
            "coverage": round(db_total / source_total, 4) if source_total else None,
        }
    return out


async def head_sample(client: EtimadClient, pool, *, pages: int, page_size: int = 50) -> dict:
    seen = healed = changed = 0
    for page in range(1, pages + 1):
        listing = await client.fetch_listing_page(page, page_size)
        for row in listing.data:
            seen += 1
            known = await pool.fetchval(
                "SELECT 1 FROM tenders WHERE source='etimad' AND source_tender_id=$1",
                row.tender_id,
            )
            event = await db.upsert_tender(pool, row, detected_by="reconcile")
            if event == "tender.created" and not known:
                healed += 1
            elif event:
                changed += 1
    miss_rate = round(healed / seen, 4) if seen else 0.0
    return {"sampled": seen, "healed_missing": healed, "updated": changed, "miss_rate": miss_rate}


async def staleness(pool, *, grace_days: int = 3) -> dict:
    stale = await pool.fetch(
        """SELECT id, source_tender_id FROM tenders
           WHERE source='etimad' AND status_id = 4
             AND last_offer_date < now() - ($1 || ' days')::interval
             AND NOT EXISTS (SELECT 1 FROM awards a WHERE a.tender_id = tenders.id)
           LIMIT 5000""",
        str(grace_days),
    )
    return {"stale_open_tenders": len(stale),
            "sample_ids": [r["source_tender_id"] for r in stale[:10]]}


async def run(pages: int) -> dict:
    pool = await db.connect(os.environ["DATABASE_URL"])
    client = EtimadClient(rate_limit_per_sec=0.5)
    run_id = await pool.fetchval(
        "INSERT INTO ingest_runs (connector) VALUES ('etimad.reconcile') RETURNING id"
    )
    error = None
    report: dict = {"at": datetime.now(timezone.utc).isoformat()}
    try:
        report["census"] = await census(client, pool)
        report["head_sample"] = await head_sample(client, pool, pages=pages)
        report["staleness"] = await staleness(pool)
        if report["head_sample"]["miss_rate"] > GAP_EVENT_THRESHOLD:
            await pool.execute(
                """INSERT INTO ingest_events (event_type, entity_type, entity_id, data)
                   VALUES ('reconcile.gap', 'system', 0, $1::jsonb)""",
                json.dumps(report["head_sample"]),
            )
        log.info("reconciliation report: %s", json.dumps(report, ensure_ascii=False))
        return report
    except Exception as exc:
        error = repr(exc)
        raise
    finally:
        await pool.execute(
            """UPDATE ingest_runs SET finished_at=now(), ok=$2, error=$3, checkpoint=$4::jsonb
               WHERE id=$1""",
            run_id, error is None, error, json.dumps(report),
        )
        await client.aclose()
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip nightly reconciliation")
    parser.add_argument("--pages", type=int, default=6, help="head-sample listing pages")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run(args.pages))


if __name__ == "__main__":
    main()
