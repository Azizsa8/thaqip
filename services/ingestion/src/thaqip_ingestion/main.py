"""Delta poll loop skeleton (ticket D2).

Usage:
  uv run python -m thaqip_ingestion.main --pages 3          # one pass, print summary
  uv run python -m thaqip_ingestion.main --loop --every 300 # continuous fast-lane poll

Persistence lands with ticket C2 (Postgres upsert + outbox). Until then the
loop validates the full fetch→normalize→diff path against an in-memory store
so the connector and models are exercised end to end.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from .etimad.client import ChallengeDetected, EtimadClient
from .normalize import classify_change, content_hash, diff_fields, to_canonical

log = logging.getLogger("thaqip.ingest")


async def run_pass(client: EtimadClient, store: dict[int, dict], *, pages: int) -> dict:
    stats = {"seen": 0, "new": 0, "changed": 0, "events": []}
    async for row in client.iter_newest(max_pages=pages):
        stats["seen"] += 1
        canonical = to_canonical(row)
        h = content_hash(canonical)
        prev = store.get(row.tender_id)
        if prev is None:
            store[row.tender_id] = {"hash": h, "canonical": canonical}
            stats["new"] += 1
            stats["events"].append(("tender.created", row.tender_id, []))
        elif prev["hash"] != h:
            changed = diff_fields(prev["canonical"], canonical)
            store[row.tender_id] = {"hash": h, "canonical": canonical}
            stats["changed"] += 1
            stats["events"].append((classify_change(changed), row.tender_id, changed))
    return stats


async def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip Etimad delta poller")
    parser.add_argument("--pages", type=int, default=2, help="newest-first pages per pass")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--every", type=int, default=300, help="seconds between passes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    client = EtimadClient()
    store: dict[int, dict] = {}
    try:
        while True:
            started = datetime.now(timezone.utc)
            try:
                stats = await run_pass(client, store, pages=args.pages)
                log.info(
                    "pass done in %.1fs seen=%s new=%s changed=%s",
                    (datetime.now(timezone.utc) - started).total_seconds(),
                    stats["seen"], stats["new"], stats["changed"],
                )
                for event_type, tender_id, changed in stats["events"][:10]:
                    log.info("event %s tender=%s changed=%s", event_type, tender_id, changed)
            except ChallengeDetected:
                log.error("bot challenge detected on listing route — backing off (ticket B5)")
                await asyncio.sleep(600)
            if not args.loop:
                break
            await asyncio.sleep(args.every)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
