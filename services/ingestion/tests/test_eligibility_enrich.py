"""Tests for thaqip_ingestion.eligibility_enrich against the real dev
database. Skips (never fabricates a pass) when Postgres is unreachable.

The Etimad HTTP fetch itself is monkeypatched to return fragments loaded
from the real fixtures test_details.py/test_tender_details.py already use,
so this stays a fast, offline test of the batch-selection/upsert logic —
the parsing itself is covered separately against the same fixtures.
"""
from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest

from thaqip_ingestion.eligibility_enrich import enrich_batch
from thaqip_ingestion.etimad.details import DetailsFetcher, TenderDetail, parse_fragment

DSN = os.environ.get("DATABASE_URL", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
async def pool():
    try:
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    except OSError as exc:
        pytest.skip(f"database unavailable: {exc}")
    yield p
    await p.close()


@pytest.fixture
async def open_tender(pool):
    """One throwaway 'etimad' tender, still open for offers, no
    tender_details row yet — exactly what enrich_batch's FIND_SQL selects."""
    row = await pool.fetchrow(
        """INSERT INTO tenders (source, source_tender_id, source_id_string, reference_number,
                                name, activity_id, booklet_price, last_offer_date, payload, content_hash)
           VALUES ('etimad', -abs(('x' || md5(random()::text))::bit(32)::int), $1,
                   'elig-test-ref', 'Eligibility enrich test tender', 111, 0,
                   now() + interval '10 days', '{}'::jsonb, md5(random()::text))
           RETURNING id""",
        f"fake-id-string-{os.getpid()}",
    )
    tender_id = row["id"]
    yield tender_id
    await pool.execute("DELETE FROM tenders WHERE id=$1", tender_id)


class _FakeFetcher:
    """Stands in for DetailsFetcher: same .fetch()/.aclose() surface, fed
    from the real fixtures with no network access."""

    def __init__(self, *_a, **_kw):
        pass

    async def aclose(self):
        pass

    async def fetch(self, tender_id_string: str) -> TenderDetail:
        relations = parse_fragment((FIX / "comp_GetRelationsDetailsViewComponenet.html").read_text())
        dates = parse_fragment((FIX / "comp_GetTenderDatesViewComponenet.html").read_text())
        return TenderDetail(tender_id_string=tender_id_string, relations=relations, dates=dates)


class _FailingFetcher(_FakeFetcher):
    async def fetch(self, tender_id_string: str) -> TenderDetail:
        raise RuntimeError("simulated bot challenge")


async def test_enrich_batch_stores_parsed_fields(monkeypatch, pool, open_tender):
    monkeypatch.setattr(
        "thaqip_ingestion.eligibility_enrich.DetailsFetcher", _FakeFetcher)
    fetched = await enrich_batch(pool, session=None, limit=10, delay=0)
    assert fetched >= 1

    row = await pool.fetchrow(
        "SELECT * FROM tender_details WHERE tender_id=$1", open_tender)
    assert row is not None
    assert row["classification_required"] is False
    assert row["execution_location"] == "داخل المملكة منطقة الرياض الرياض"
    assert row["fetch_error"] is None


async def test_enrich_batch_never_leaves_a_broken_tender_silently_unrecorded(
    monkeypatch, pool, open_tender
):
    monkeypatch.setattr(
        "thaqip_ingestion.eligibility_enrich.DetailsFetcher", _FailingFetcher)
    fetched = await enrich_batch(pool, session=None, limit=10, delay=0)
    assert fetched == 0  # failures are not counted as fetched

    row = await pool.fetchrow(
        "SELECT * FROM tender_details WHERE tender_id=$1", open_tender)
    assert row is not None
    assert row["fetch_error"] is not None
    assert row["classification_required"] is None


async def test_enrich_batch_skips_tenders_that_already_have_details(
    monkeypatch, pool, open_tender
):
    await pool.execute(
        "INSERT INTO tender_details (tender_id, classification_text) VALUES ($1, 'قديم')",
        open_tender,
    )
    monkeypatch.setattr(
        "thaqip_ingestion.eligibility_enrich.DetailsFetcher", _FakeFetcher)

    await enrich_batch(pool, session=None, limit=10, delay=0)

    row = await pool.fetchrow(
        "SELECT classification_text FROM tender_details WHERE tender_id=$1", open_tender)
    # Already-enriched, so FIND_SQL must not have re-selected it — the
    # pre-existing value is untouched, not overwritten by the fake fetch.
    assert row["classification_text"] == "قديم"
