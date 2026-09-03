"""Checkpointed historical backfill (ticket C4).

Walks the visitor listing page by page (optionally the awarded category,
TenderCategory=6) and upserts every tender. Progress is checkpointed into
ingest_runs.checkpoint after every page, so a crash or rate-limit abort
RESUMES from the last completed page instead of restarting.

Run:
  DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.backfill \
      --category awarded --max-pages 40
  # re-running the same command continues from the stored checkpoint.

Throughput note: the listing API tolerates ~1 req/s; at PageSize=50 a full
walk of ~288k tenders is ~5,800 pages ≈ 2 hours. The awarding *components*
(offers/awards, ticket B4) are the slow lane (~3s each) and are harvested by
awards_harvest separately — this job builds the tender corpus fast first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

import asyncpg

from . import db
from .etimad.client import EtimadClient

log = logging.getLogger("thaqip.backfill")

CATEGORY_PARAMS: dict[str, dict] = {
    "all": {},
    "awarded": {"TenderCategory": 6},   # "تم اعلان الترسية"
    "finished": {"TenderCategory": 8},  # "المنافسات المنتهية"
}


async def load_checkpoint(pool: asyncpg.Pool, connector: str) -> int:
    """Return the last completed page for this connector (0 if none)."""
    row = await pool.fetchrow(
        """SELECT checkpoint FROM ingest_runs
           WHERE connector = $1 AND checkpoint IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        connector,
    )
    if row and row["checkpoint"]:
        cp = row["checkpoint"]
        if isinstance(cp, str):
            cp = json.loads(cp)
        return int(cp.get("last_page", 0))
    return 0


async def run_backfill(
    pool: asyncpg.Pool,
    *,
    category: str,
    max_pages: int,
    page_size: int = 50,
    rate_limit: float = 1.0,
) -> dict:
    connector = f"etimad.backfill.{category}"
    start_page = await load_checkpoint(pool, connector) + 1
    log.info("backfill %s: resuming at page %d", connector, start_page)

    client = EtimadClient(rate_limit_per_sec=rate_limit)
    stats = {"pages": 0, "seen": 0, "new": 0, "changed": 0, "total_count": None}
    run_id = await pool.fetchval(
        "INSERT INTO ingest_runs (connector) VALUES ($1) RETURNING id", connector
    )
    error: str | None = None
    try:
        for page in range(start_page, start_page + max_pages):
            listing = await client.fetch_listing_page(
                page, page_size, extra_params=CATEGORY_PARAMS[category]
            )
            stats["total_count"] = listing.totalCount
            if not listing.data:
                log.info("empty page %d — corpus walk complete", page)
                break
            for row in listing.data:
                stats["seen"] += 1
                event = await db.upsert_tender(pool, row, detected_by="backfill")
                if event == "tender.created":
                    stats["new"] += 1
                elif event:
                    stats["changed"] += 1
            stats["pages"] += 1
            await pool.execute(
                """UPDATE ingest_runs SET pages = $2, items_seen = $3, items_new = $4,
                       items_changed = $5, checkpoint = $6::jsonb WHERE id = $1""",
                run_id, stats["pages"], stats["seen"], stats["new"], stats["changed"],
                json.dumps({"last_page": page, "page_size": page_size}),
            )
            if page % 10 == 0:
                log.info("page %d/%s done: %s", page,
                         (listing.totalCount + page_size - 1) // page_size, stats)
    except Exception as exc:
        error = repr(exc)
        raise
    finally:
        await pool.execute(
            "UPDATE ingest_runs SET finished_at = now(), ok = $2, error = $3 WHERE id = $1",
            run_id, error is None, error,
        )
        await client.aclose()
    return stats


async def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip historical backfill")
    parser.add_argument("--category", choices=sorted(CATEGORY_PARAMS), default="all")
    parser.add_argument("--max-pages", type=int, default=100, help="pages this session")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--rate", type=float, default=1.0, help="requests/second")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    pool = await db.connect(os.environ["DATABASE_URL"])
    try:
        stats = await run_backfill(
            pool, category=args.category, max_pages=args.max_pages,
            page_size=args.page_size, rate_limit=args.rate,
        )
        log.info("backfill session done: %s", stats)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
