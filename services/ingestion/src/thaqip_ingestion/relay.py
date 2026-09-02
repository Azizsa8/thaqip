"""Outbox -> Redis Streams relay (ticket D1).

At-least-once delivery: rows are XADDed to the stream, then marked relayed in
Postgres. A crash between the two steps re-delivers on restart — consumers
must dedupe on `event_id` (which is the outbox primary key and is monotonic).

Stream: `thaqip.events` (single stream; consumers filter by event_type, or we
split per-type streams when a consumer needs it).

Run:
  DATABASE_URL=... REDIS_URL=redis://localhost:6380/0 \
      uv run --extra db python -m thaqip_ingestion.relay [--once]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

import asyncpg
import redis.asyncio as aioredis

log = logging.getLogger("thaqip.relay")

STREAM = "thaqip.events"
BATCH = 200
MAXLEN = 1_000_000  # keep stream bounded; consumers should stay near the head


async def relay_batch(pool: asyncpg.Pool, r: aioredis.Redis) -> int:
    rows = await pool.fetch(
        """SELECT id, event_type, entity_type, entity_id, data, created_at
           FROM ingest_events WHERE relayed_at IS NULL ORDER BY id LIMIT $1""",
        BATCH,
    )
    if not rows:
        return 0
    pipe = r.pipeline(transaction=False)
    for row in rows:
        pipe.xadd(
            STREAM,
            {
                "event_id": str(row["id"]),
                "event_type": row["event_type"],
                "entity_type": row["entity_type"],
                "entity_id": str(row["entity_id"]),
                "data": row["data"] if isinstance(row["data"], str) else json.dumps(row["data"]),
                "created_at": row["created_at"].isoformat(),
            },
            maxlen=MAXLEN,
            approximate=True,
        )
    await pipe.execute()
    await pool.execute(
        "UPDATE ingest_events SET relayed_at = now() WHERE id = ANY($1::bigint[])",
        [row["id"] for row in rows],
    )
    log.info("relayed %d events (last id %d)", len(rows), rows[-1]["id"])
    return len(rows)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip outbox relay")
    parser.add_argument("--once", action="store_true", help="drain once and exit")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from . import db

    pool = await db.connect(os.environ["DATABASE_URL"])
    r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6380/0"))
    try:
        while True:
            n = await relay_batch(pool, r)
            if args.once and n < BATCH:
                break
            if n == 0:
                await asyncio.sleep(args.interval)
    finally:
        await r.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
