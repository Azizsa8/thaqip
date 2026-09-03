"""Search indexer (ticket E2): Typesense over tenders + extracted document text.

Two modes:
  --bulk    (re)index the whole corpus — idempotent, run after schema changes
  (default) follow `thaqip.events` as consumer group 'indexer' and upsert
            changed tenders as events arrive

Document text: all doc_chunks for the tender concatenated (capped) into one
searchable field, so an Arabic phrase that appears only inside a BOQ/TSD file
still finds its tender — the search feature Etimad itself lacks (M2-2).

Run:
  DATABASE_URL=... TYPESENSE_URL=http://localhost:8108 TYPESENSE_KEY=... \
      uv run --extra db python -m thaqip_ingestion.indexer --bulk
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

import asyncpg
import httpx

from . import db

log = logging.getLogger("thaqip.indexer")

COLLECTION = "tenders"
DOC_TEXT_CAP = 60_000

SCHEMA = {
    "name": COLLECTION,
    "fields": [
        {"name": "name", "type": "string"},
        {"name": "reference", "type": "string"},
        {"name": "agency", "type": "string", "optional": True},
        {"name": "activity", "type": "string", "optional": True},
        {"name": "doc_text", "type": "string", "optional": True},
        {"name": "source", "type": "string", "facet": True},
        {"name": "has_award", "type": "bool", "facet": True},
        {"name": "open_now", "type": "bool", "facet": True},
        {"name": "last_offer_ts", "type": "int64", "optional": True},
    ],
    "default_sorting_field": "",
}

ROW_SQL = """
SELECT t.id, t.name, t.reference_number, t.source,
       coalesce(a.canonical_name, t.agency_name_raw) AS agency,
       t.activity_name_raw AS activity,
       t.last_offer_date,
       t.last_offer_date > now() AS open_now,
       EXISTS (SELECT 1 FROM awards w WHERE w.tender_id = t.id) AS has_award,
       (SELECT left(string_agg(c.content, ' '), {cap})
          FROM doc_chunks c JOIN documents d ON d.id = c.document_id
         WHERE d.tender_id = t.id) AS doc_text
FROM tenders t LEFT JOIN agencies a ON a.id = t.agency_id
{where}
""".replace("{cap}", str(DOC_TEXT_CAP))


class Typesense:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=os.environ.get("TYPESENSE_URL", "http://localhost:8108"),
            headers={"X-TYPESENSE-API-KEY": os.environ.get("TYPESENSE_KEY", "thaqip_dev_search")},
            timeout=60,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ensure_collection(self) -> None:
        r = await self._http.get(f"/collections/{COLLECTION}")
        if r.status_code == 404:
            (await self._http.post("/collections", json=SCHEMA)).raise_for_status()
            log.info("collection created")

    async def upsert_batch(self, docs: list[dict]) -> int:
        if not docs:
            return 0
        payload = "\n".join(json.dumps(d, ensure_ascii=False) for d in docs)
        r = await self._http.post(
            f"/collections/{COLLECTION}/documents/import",
            params={"action": "upsert"}, content=payload.encode(),
        )
        r.raise_for_status()
        ok = sum(1 for line in r.text.splitlines() if '"success":true' in line)
        return ok


def to_doc(row) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"] or "",
        "reference": row["reference_number"] or "",
        "agency": row["agency"] or "",
        "activity": row["activity"] or "",
        "doc_text": row["doc_text"] or "",
        "source": row["source"],
        "has_award": bool(row["has_award"]),
        "open_now": bool(row["open_now"]),
        "last_offer_ts": int(row["last_offer_date"].timestamp()) if row["last_offer_date"] else 0,
    }


async def bulk(pool: asyncpg.Pool, ts: Typesense, batch: int = 500) -> int:
    total = 0
    last_id = 0
    while True:
        rows = await pool.fetch(
            ROW_SQL.format(where="WHERE t.id > $1 ORDER BY t.id LIMIT $2"), last_id, batch
        )
        if not rows:
            break
        total += await ts.upsert_batch([to_doc(r) for r in rows])
        last_id = rows[-1]["id"]
    log.info("bulk indexed %d documents", total)
    return total


async def follow(pool: asyncpg.Pool, ts: Typesense) -> None:
    import redis.asyncio as aioredis

    r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6380/0"),
                          decode_responses=True)
    try:
        await r.xgroup_create("thaqip.events", "indexer", id="$", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
    log.info("indexer following thaqip.events")
    while True:
        try:
            resp = await r.xreadgroup("indexer", "worker-1", {"thaqip.events": ">"},
                                      count=100, block=5000)
        except (TimeoutError, aioredis.TimeoutError, aioredis.ConnectionError) as exc:
            log.debug("blocking read cycle: %r", exc)
            await asyncio.sleep(1)
            continue
        if not resp:
            continue
        ids: set[int] = set()
        entries = []
        for _s, es in resp:
            for entry_id, fields in es:
                entries.append(entry_id)
                if fields.get("entity_type") == "tender":
                    ids.add(int(fields["entity_id"]))
                elif fields.get("event_type") == "document.stored":
                    data = json.loads(fields.get("data") or "{}")
                    if data.get("tender_id"):
                        ids.add(int(data["tender_id"]))
        if ids:
            rows = await pool.fetch(
                ROW_SQL.format(where="WHERE t.id = ANY($1)"), list(ids)
            )
            n = await ts.upsert_batch([to_doc(r_) for r_ in rows])
            log.info("reindexed %d tenders", n)
        for e in entries:
            await r.xack("thaqip.events", "indexer", e)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip search indexer")
    parser.add_argument("--bulk", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    ts = Typesense()
    try:
        await ts.ensure_collection()
        if args.bulk:
            await bulk(pool, ts)
        else:
            await follow(pool, ts)
    finally:
        await ts.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
