"""Delta poll loop (ticket D2), Postgres-backed (ticket C2).

Usage:
  DATABASE_URL=postgres://thaqip:thaqip_dev@localhost:5433/thaqip \
    uv run python -m thaqip_ingestion.main --pages 3            # one pass
  ... --loop --every 300                                        # continuous fast lane

Without DATABASE_URL the loop falls back to an in-memory store (useful for
connector smoke tests without infrastructure).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, timezone

from .etimad.client import ChallengeDetected, EtimadClient
from .normalize import classify_change, content_hash, diff_fields, to_canonical

log = logging.getLogger("thaqip.ingest")


async def run_pass_memory(client: EtimadClient, store: dict[int, dict], *, pages: int) -> Counter:
    stats: Counter = Counter()
    async for row in client.iter_newest(max_pages=pages):
        stats["seen"] += 1
        canonical = to_canonical(row)
        h = content_hash(canonical)
        prev = store.get(row.tender_id)
        if prev is None:
            store[row.tender_id] = {"hash": h, "canonical": canonical}
            stats["tender.created"] += 1
        elif prev["hash"] != h:
            changed = diff_fields(prev["canonical"], canonical)
            store[row.tender_id] = {"hash": h, "canonical": canonical}
            stats[classify_change(changed)] += 1
    return stats


async def run_pass_db(client: EtimadClient, pool, *, pages: int) -> Counter:
    from . import db

    stats: Counter = Counter()
    error: str | None = None
    try:
        async for row in client.iter_newest(max_pages=pages):
            stats["seen"] += 1
            event = await db.upsert_tender(pool, row, detected_by="poller")
            if event:
                stats[event] += 1
    except Exception as exc:  # record the failed run, then surface it
        error = repr(exc)
        raise
    finally:
        new = stats.get("tender.created", 0)
        changed = sum(v for k, v in stats.items() if k.startswith("tender.") and k != "tender.created")
        await db.record_run(
            pool, connector="etimad.listing", ok=error is None, pages=pages,
            seen=stats.get("seen", 0), new=new, changed=changed, error=error,
        )
    return stats


async def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip Etimad delta poller")
    parser.add_argument("--pages", type=int, default=2, help="newest-first pages per pass")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--every", type=int, default=300, help="seconds between passes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    dsn = os.environ.get("DATABASE_URL")
    pool = None
    if dsn:
        from . import db

        pool = await db.connect(dsn)
        log.info("postgres-backed mode")
    else:
        log.info("in-memory mode (set DATABASE_URL for persistence)")

    client = EtimadClient()
    mem_store: dict[int, dict] = {}
    try:
        while True:
            started = datetime.now(timezone.utc)
            try:
                if pool is not None:
                    stats = await run_pass_db(client, pool, pages=args.pages)
                else:
                    stats = await run_pass_memory(client, mem_store, pages=args.pages)
                log.info(
                    "pass done in %.1fs %s",
                    (datetime.now(timezone.utc) - started).total_seconds(),
                    dict(stats),
                )
            except ChallengeDetected:
                log.error("bot challenge detected on listing route — backing off (ticket B5)")
                await asyncio.sleep(600)
            if not args.loop:
                break
            await asyncio.sleep(args.every)
    finally:
        await client.aclose()
        if pool is not None:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
