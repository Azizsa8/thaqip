"""Forsah connector (ticket B6 — spike promoted to working listing pull).

Verified 2026-09-03: `https://forsah-api.910ths.sa/api/v1` is a public JSON
API — no auth, no bot protection. Key endpoints:

  GET /opportunities?page=&size=      paginated listing (25k+ rows)
  GET /opportunities/count            {count, totalAwardedValue, totalPublishedValue}
  GET /inquiries?...                  Q&A threads (P1 follow-up)

Each opportunity carries the competition-intensity counters the incumbent
markets as premium: bidsCount, submittedBidsCount, submittedExternalBidsCount,
draftBidsCount — plus bilingual categories, delivery cities, value ranges,
dueDate/awardDate. Ids are UUIDs (schema migration 0002).

Run:  DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.forsah --pages 3
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any

import httpx

from . import db
from .db import KSA_TZ


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=KSA_TZ)

log = logging.getLogger("thaqip.forsah")

BASE = "https://forsah-api.910ths.sa/api/v1"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}

EVENTFUL = ("status_id", "last_offer_date", "submitted_bids_count")


def normalize(r: dict[str, Any]) -> dict[str, Any]:
    cats = r.get("categories") or []
    cities = [dl.get("city", {}).get("name", {}).get("ar")
              for dl in (r.get("deliveryLocations") or []) if dl.get("city")]
    return {
        "source": "forsah",
        "source_uid": r["id"],
        "reference_number": r["id"][:13],
        "name": r.get("title") or "",
        "agency_name_raw": (r.get("publisher") or {}).get("publisherType"),
        "activity_name_raw": (cats[0].get("name", {}).get("ar") if cats else None),
        "tender_type_name": ((r.get("type") or {}).get("name") or {}).get("ar"),
        "status_name": r.get("statusKey"),
        "status_id": {"open": 4, "awarded": 15, "closed": 8}.get(r.get("statusKey"), 0),
        "published_at": r.get("publishDate"),
        "last_offer_date": r.get("extendedDueDate") or r.get("dueDate"),
        "branch_name": "، ".join(c for c in cities if c) or None,
        "bids_count": r.get("bidsCount"),
        "submitted_bids_count": r.get("submittedBidsCount"),
        "external_bids_count": r.get("submittedExternalBidsCount"),
        "draft_bids_count": r.get("draftBidsCount"),
    }


def content_hash(c: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(c, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


UPSERT = """
INSERT INTO tenders (source, source_uid, reference_number, name, agency_name_raw,
                     activity_name_raw, tender_type_name, status_name, status_id,
                     published_at, last_offer_date, branch_name,
                     bids_count, submitted_bids_count, external_bids_count, draft_bids_count,
                     payload, content_hash)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,
        $10::timestamptz,$11::timestamptz,$12,$13,$14,$15,$16,$17::jsonb,$18)
ON CONFLICT (source, source_uid) WHERE source_uid IS NOT NULL DO NOTHING
RETURNING id
"""


async def upsert(pool, raw: dict[str, Any]) -> str | None:
    c = normalize(raw)
    h = content_hash(c)
    payload = json.dumps(raw, ensure_ascii=False)
    args = [c["source"], c["source_uid"], c["reference_number"], c["name"],
            c["agency_name_raw"], c["activity_name_raw"], c["tender_type_name"],
            c["status_name"], c["status_id"], _ts(c["published_at"]), _ts(c["last_offer_date"]),
            c["branch_name"], c["bids_count"], c["submitted_bids_count"],
            c["external_bids_count"], c["draft_bids_count"], payload, h]
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, content_hash FROM tenders WHERE source='forsah' AND source_uid=$1",
            c["source_uid"],
        )
        if row is None:
            new = await conn.fetchrow(UPSERT, *args)
            if new is None:
                return None
            await conn.execute(
                """INSERT INTO ingest_events (event_type, entity_type, entity_id, data)
                       VALUES ('tender.created', 'tender', $1, '{"source":"forsah"}')""",
                new["id"],
            )
            return "tender.created"
        if row["content_hash"] == h:
            return None
        prev_submitted = await conn.fetchval(
            "SELECT submitted_bids_count FROM tenders WHERE id=$1", row["id"]
        )
        await conn.execute(
            """UPDATE tenders SET name=$2, status_name=$3, status_id=$4,
                       last_offer_date=$5::timestamptz, bids_count=$6,
                       submitted_bids_count=$7, external_bids_count=$8, draft_bids_count=$9,
                       payload=$10::jsonb, content_hash=$11, updated_at=now()
                   WHERE id=$1""",
            row["id"], c["name"], c["status_name"], c["status_id"],
            _ts(c["last_offer_date"]), c["bids_count"], c["submitted_bids_count"],
            c["external_bids_count"], c["draft_bids_count"], payload, h,
        )
        event = "tender.awarded" if c["status_id"] == 15 else "tender.updated"
        data: dict[str, Any] = {"source": "forsah"}
        # competition.rising: submitted bids increased on a tender the team is
        # actively pursuing — the cheap differentiator from the B6 spike.
        new_submitted = c["submitted_bids_count"] or 0
        if (prev_submitted is not None and new_submitted > prev_submitted
                and await conn.fetchval(
                    "SELECT 1 FROM pursuits WHERE tender_id=$1", row["id"])):
            event = "competition.rising"
            data.update({"from": prev_submitted, "to": new_submitted})
        await conn.execute(
            """INSERT INTO ingest_events (event_type, entity_type, entity_id, data)
                   VALUES ($1, 'tender', $2, $3::jsonb)""",
            event, row["id"], json.dumps(data, ensure_ascii=False),
        )
        return event


async def run(pages: int, size: int = 25) -> dict:
    stats = {"seen": 0, "created": 0, "updated": 0}
    pool = await db.connect(os.environ["DATABASE_URL"])
    async with httpx.AsyncClient(headers=HEADERS, timeout=45) as h:
        try:
            for page in range(1, pages + 1):
                resp = await h.get(f"{BASE}/opportunities", params={"page": page, "size": size})
                resp.raise_for_status()
                for raw in resp.json().get("result", []):
                    stats["seen"] += 1
                    ev = await upsert(pool, raw)
                    if ev == "tender.created":
                        stats["created"] += 1
                    elif ev:
                        stats["updated"] += 1
                await asyncio.sleep(1.5)
        finally:
            await pool.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Thaqip Forsah listing pull")
    parser.add_argument("--pages", type=int, default=2)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    stats = asyncio.run(run(args.pages))
    log.info("forsah pull done: %s", stats)


if __name__ == "__main__":
    main()
