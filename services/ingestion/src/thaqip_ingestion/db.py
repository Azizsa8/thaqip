"""Postgres persistence for the ingestion loop (ticket C2 + D1 outbox write side).

Contract: tender upsert and its outbox event commit in ONE transaction, so an
event exists iff the row change it describes was durably stored. Replaying an
unchanged row performs no write and emits no event (idempotency).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

KSA_TZ = ZoneInfo("Asia/Riyadh")  # Etimad timestamps are local KSA time when naive

from .etimad.models import EtimadTenderRow
from .normalize import classify_change, content_hash, diff_fields, to_canonical

UPSERT_COLUMNS = [
    "source", "source_tender_id", "source_id_string", "reference_number", "tender_number",
    "name", "agency_name_raw", "branch_name", "activity_id", "activity_name_raw",
    "tender_type_id", "tender_type_name", "status_id", "status_name",
    "booklet_price", "financial_fees", "buying_cost", "invitation_cost",
    "submission_date", "last_enquiries_date", "last_offer_date", "offers_opening_date",
    "last_enquiries_date_hijri", "last_offer_date_hijri", "offers_opening_date_hijri",
    "inside_ksa", "published_at",
]
_TS_COLUMNS = {"submission_date", "last_enquiries_date", "last_offer_date",
               "offers_opening_date", "published_at"}
_NUM_COLUMNS = {"booklet_price", "financial_fees", "buying_cost", "invitation_cost"}


def _cast(col: str) -> str:
    if col in _TS_COLUMNS:
        return "::timestamptz"
    if col in _NUM_COLUMNS:
        return "::numeric"
    return ""


_INSERT_SQL = f"""
INSERT INTO tenders ({", ".join(UPSERT_COLUMNS)}, payload, content_hash)
VALUES ({", ".join(f"${i + 1}{_cast(c)}" for i, c in enumerate(UPSERT_COLUMNS))},
        ${len(UPSERT_COLUMNS) + 1}::jsonb, ${len(UPSERT_COLUMNS) + 2})
ON CONFLICT (source, source_tender_id) DO NOTHING
RETURNING id
"""

_SELECT_SQL = """
SELECT id, content_hash, payload FROM tenders
WHERE source = $1 AND source_tender_id = $2
"""

_UPDATE_SQL = f"""
UPDATE tenders SET
  {", ".join(f"{c} = ${i + 1}{_cast(c)}" for i, c in enumerate(UPSERT_COLUMNS))},
  payload = ${len(UPSERT_COLUMNS) + 1}::jsonb,
  content_hash = ${len(UPSERT_COLUMNS) + 2},
  updated_at = now()
WHERE id = ${len(UPSERT_COLUMNS) + 3}
"""

_EVENT_SQL = """
INSERT INTO ingest_events (event_type, entity_type, entity_id, data)
VALUES ($1, 'tender', $2, $3::jsonb)
"""


async def connect(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=4)


def _row_args(canonical: dict[str, Any]) -> list[Any]:
    out: list[Any] = []
    for c in UPSERT_COLUMNS:
        v = canonical[c]
        if c in _NUM_COLUMNS and v is not None:
            v = str(v)  # sent as text, cast to numeric server-side (avoids float rounding)
        elif c in _TS_COLUMNS and v is not None:
            dt = datetime.fromisoformat(v)
            v = dt if dt.tzinfo else dt.replace(tzinfo=KSA_TZ)
        out.append(v)
    return out


async def upsert_tender(pool: asyncpg.Pool, row: EtimadTenderRow) -> str | None:
    """Insert or update one tender. Returns the emitted event type, or None."""
    canonical = to_canonical(row)
    new_hash = content_hash(canonical)
    payload = row.model_dump_json(by_alias=True)

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(_SELECT_SQL, "etimad", row.tender_id)
            if existing is None:
                inserted = await conn.fetchrow(_INSERT_SQL, *_row_args(canonical), payload, new_hash)
                if inserted is None:  # lost a concurrent-insert race; treat as replay
                    return None
                await conn.execute(_EVENT_SQL, "tender.created", inserted["id"], json.dumps({}))
                return "tender.created"

            if existing["content_hash"] == new_hash:
                return None

            old_canonical = to_canonical(
                EtimadTenderRow.model_validate_json(existing["payload"])
            )
            changed = diff_fields(old_canonical, canonical)
            event_type = classify_change(changed)
            await conn.execute(_UPDATE_SQL, *_row_args(canonical), payload, new_hash, existing["id"])
            await conn.execute(
                _EVENT_SQL, event_type, existing["id"], json.dumps({"changed_fields": changed})
            )
            return event_type


async def record_run(
    pool: asyncpg.Pool, *, connector: str, ok: bool,
    pages: int, seen: int, new: int, changed: int, error: str | None = None,
) -> None:
    await pool.execute(
        """INSERT INTO ingest_runs (connector, finished_at, ok, pages, items_seen, items_new, items_changed, error)
           VALUES ($1, now(), $2, $3, $4, $5, $6, $7)""",
        connector, ok, pages, seen, new, changed, error,
    )
