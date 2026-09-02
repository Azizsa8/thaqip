"""Entity resolution v1 (ticket C3): agencies and vendors.

Arabic organization names vary by orthography (hamza forms, taa marbuta,
alef maqsura, tatweel, diacritics) and spacing. We canonicalize with a
normalization key, keep every observed variant, and link tenders/offers to
stable entity ids. Merges are conservative: only rows whose normalization
keys match exactly are unified in v1; fuzzy merging waits for a labeled set.

Run:
  DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.resolution
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from . import db

log = logging.getLogger("thaqip.resolution")

_DIACRITICS = re.compile(r"[ً-ٰٟـ]")  # harakat + tatweel
_WS = re.compile(r"\s+")

# Common legal-form prefixes that don't distinguish entities when comparing.
_PREFIXES = ("شركة", "مؤسسة", "مكتب", "مجموعة")


def normalize_ar(name: str) -> str:
    """Normalization key for Arabic org names (NOT a display form)."""
    s = _DIACRITICS.sub("", name)
    s = (s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
          .replace("ى", "ي").replace("ة", "ه").replace("ئ", "ي").replace("ؤ", "و"))
    s = _WS.sub(" ", s).strip()
    return s


def match_key(name: str) -> str:
    """Stricter key used for equality matching (drops legal-form prefix)."""
    s = normalize_ar(name)
    for p in _PREFIXES_NORM:
        if s.startswith(p + " "):
            s = s[len(p) + 1:]
            break
    return s


_PREFIXES_NORM = tuple(normalize_ar(p) for p in _PREFIXES)


async def resolve_agencies(pool) -> dict:
    """Canonicalize tenders.agency_name_raw into agencies + link tenders.agency_id."""
    stats = {"agencies": 0, "linked": 0}
    rows = await pool.fetch(
        """SELECT DISTINCT agency_name_raw FROM tenders
           WHERE agency_name_raw IS NOT NULL AND agency_id IS NULL"""
    )
    by_key: dict[str, list[str]] = {}
    for r in rows:
        by_key.setdefault(normalize_ar(r["agency_name_raw"]), []).append(r["agency_name_raw"])

    async with pool.acquire() as conn:
        for key, variants in by_key.items():
            display = max(variants, key=len)  # richest observed spelling as display form
            async with conn.transaction():
                agency_id = await conn.fetchval(
                    """INSERT INTO agencies (canonical_name, name_variants)
                       VALUES ($1, $2)
                       ON CONFLICT (canonical_name) DO UPDATE
                         SET name_variants = (
                           SELECT array(SELECT DISTINCT unnest(agencies.name_variants || EXCLUDED.name_variants))
                         )
                       RETURNING id""",
                    display, variants,
                )
                n = await conn.execute(
                    "UPDATE tenders SET agency_id = $1 WHERE agency_name_raw = ANY($2) AND agency_id IS NULL",
                    agency_id, variants,
                )
                stats["agencies"] += 1
                stats["linked"] += int(n.split()[-1])
    return stats


async def dedupe_vendors(pool) -> dict:
    """Merge vendor rows whose match_key collides; repoint offers/awards."""
    stats = {"groups_merged": 0, "rows_merged": 0}
    vendors = await pool.fetch("SELECT id, canonical_name FROM vendors ORDER BY id")
    groups: dict[str, list] = {}
    for v in vendors:
        groups.setdefault(match_key(v["canonical_name"]), []).append(v)

    async with pool.acquire() as conn:
        for _key, members in groups.items():
            if len(members) < 2:
                continue
            keeper, *dupes = members  # lowest id wins; earliest observation is stable
            async with conn.transaction():
                dupe_ids = [d["id"] for d in dupes]
                await conn.execute(
                    "UPDATE offers SET vendor_id = $1 WHERE vendor_id = ANY($2)", keeper["id"], dupe_ids
                )
                await conn.execute(
                    "UPDATE awards SET vendor_id = $1 WHERE vendor_id = ANY($2)", keeper["id"], dupe_ids
                )
                await conn.execute(
                    """UPDATE vendors SET name_variants = (
                         SELECT array(SELECT DISTINCT unnest(name_variants || $2::text[]))
                       ) WHERE id = $1""",
                    keeper["id"], [d["canonical_name"] for d in dupes],
                )
                await conn.execute("DELETE FROM vendors WHERE id = ANY($1)", dupe_ids)
                stats["groups_merged"] += 1
                stats["rows_merged"] += len(dupes)
    return stats


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    try:
        a = await resolve_agencies(pool)
        v = await dedupe_vendors(pool)
        log.info("agencies: %s | vendors: %s", a, v)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
