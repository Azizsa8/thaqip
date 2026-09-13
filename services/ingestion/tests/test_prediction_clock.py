"""Accuracy Stage 1: the prediction clock and its blindness rule.

Runs against the live dev database inside a transaction that is rolled back,
so no fixture rows survive. The rule under test lives in the
prediction_scorecard view (migration 0019); these tests pin the cases that
would silently inflate accuracy if they regressed.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("DATABASE_URL", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")
T0 = datetime(2026, 1, 10, 9, 0, tzinfo=UTC)


async def _in_rollback(body):
    try:
        conn = await asyncpg.connect(DSN)
    except OSError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")
    try:
        if not await conn.fetchval("SELECT to_regclass('prediction_scorecard') IS NOT NULL"):
            pytest.skip("migration 0019 not applied")
        tx = conn.transaction()
        await tx.start()
        try:
            return await body(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


async def _tender(conn, *, opening=T0 + timedelta(days=5)):
    return await conn.fetchval(
        """INSERT INTO tenders (source, source_tender_id, reference_number, name, payload,
                                content_hash, offers_opening_date, last_offer_date)
           VALUES ('etimad', -(floor(random()*1e12))::bigint, 'clock-battery-' || gen_random_uuid(),
                   'clock-battery', '{}'::jsonb, md5(random()::text), $1, $1)
           RETURNING id""", opening)


async def _predict(conn, tender_id, at, *, p10=90_000, p50=100_000, p90=110_000,
                   suppressed=False, origin="daily_snapshot"):
    if suppressed:
        p10 = p50 = p90 = None
    return await conn.fetchval(
        """INSERT INTO price_predictions (tender_id, prediction_scope, p10, p50, p90,
               model_version, suppression_reason, generated_at, origin, snapshot_date)
           VALUES ($1, 'MARKET', $2, $3, $4, 'battery', $5, $6::timestamptz, $7, ($6::timestamptz AT TIME ZONE 'UTC')::date)
           RETURNING id""",
        tender_id, p10, p50, p90, "INSUFFICIENT_EVIDENCE" if suppressed else None, at, origin)


async def _award(conn, tender_id, value, first_seen, *, awardees=1):
    for _ in range(awardees):
        await conn.execute(
            "INSERT INTO awards (tender_id, award_value) VALUES ($1, $2)", tender_id, value)
    await conn.execute(
        "INSERT INTO tender_award_first_seen (tender_id, first_seen) VALUES ($1, $2)",
        tender_id, first_seen)


async def _card(conn, tender_id):
    return await conn.fetchrow("SELECT * FROM prediction_scorecard WHERE tender_id = $1", tender_id)


def test_a_prediction_made_before_offers_opened_is_scored():
    async def body(conn):
        tid = await _tender(conn)
        pid = await _predict(conn, tid, T0)
        await _award(conn, tid, 105_000, T0 + timedelta(days=20))
        row = await _card(conn, tid)
        assert row["prediction_id"] == pid and row["scorable"]
        assert row["interval_hit"] is True
        assert float(row["abs_pct_error"]) == pytest.approx(5_000 / 105_000, abs=1e-4)
        assert float(row["lead_days"]) == pytest.approx(5.0, abs=0.1)
    asyncio.run(_in_rollback(body))


def test_a_prediction_made_after_offers_opened_is_not_blind():
    """Competitor prices can be public from offers opening; a prediction made
    then is not evidence of skill even if the award is not yet known."""
    async def body(conn):
        tid = await _tender(conn, opening=T0)
        await _predict(conn, tid, T0 + timedelta(hours=1))
        await _award(conn, tid, 100_000, T0 + timedelta(days=20))
        assert await _card(conn, tid) is None
    asyncio.run(_in_rollback(body))


def test_a_prediction_made_after_the_award_was_seen_is_not_blind():
    async def body(conn):
        tid = await _tender(conn, opening=None)
        await _predict(conn, tid, T0 + timedelta(days=3))
        await _award(conn, tid, 100_000, T0)
        assert await _card(conn, tid) is None
    asyncio.run(_in_rollback(body))


def test_only_the_latest_blind_prediction_counts_once_per_tender():
    async def body(conn):
        tid = await _tender(conn)
        await _predict(conn, tid, T0, p10=1, p50=2, p90=3)
        latest = await _predict(conn, tid, T0 + timedelta(days=1))
        await _predict(conn, tid, T0 + timedelta(days=6), origin="interactive")  # after opening
        await _award(conn, tid, 100_000, T0 + timedelta(days=9))
        rows = await conn.fetch("SELECT prediction_id FROM prediction_scorecard WHERE tender_id=$1", tid)
        assert [r["prediction_id"] for r in rows] == [latest]
    asyncio.run(_in_rollback(body))


def test_refusals_are_recorded_but_never_scored_as_hits_or_misses():
    async def body(conn):
        tid = await _tender(conn)
        await _predict(conn, tid, T0, suppressed=True)
        await _award(conn, tid, 100_000, T0 + timedelta(days=9))
        row = await _card(conn, tid)
        assert row["suppressed"] and not row["scorable"]
        assert row["interval_hit"] is None and row["abs_pct_error"] is None
    asyncio.run(_in_rollback(body))


def test_multi_awardee_tenders_are_excluded_from_scoring():
    async def body(conn):
        tid = await _tender(conn)
        await _predict(conn, tid, T0)
        await _award(conn, tid, 50_000, T0 + timedelta(days=9), awardees=2)
        assert not (await _card(conn, tid))["scorable"]
    asyncio.run(_in_rollback(body))


def test_one_daily_snapshot_per_tender_per_day_is_enforced_by_the_database():
    async def body(conn):
        tid = await _tender(conn)
        await _predict(conn, tid, T0)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _predict(conn, tid, T0 + timedelta(hours=3))
    asyncio.run(_in_rollback(body))


def test_reharvesting_awards_does_not_move_first_seen():
    """awards rows are deleted and re-inserted on each harvest; the cutoff must not follow them."""
    from thaqip_ingestion.awards_harvest import store_awarding
    from thaqip_ingestion.etimad.awards import Awardee, AwardingResult, Bidder

    async def body(conn):
        tid = await _tender(conn)
        await conn.execute(
            "INSERT INTO tender_award_first_seen (tender_id, first_seen) VALUES ($1, $2)", tid, T0)

        class OneConn:  # store_awarding expects a pool; hand it this transaction's connection
            def acquire(self):
                outer = conn

                class Ctx:
                    async def __aenter__(self):
                        return outer

                    async def __aexit__(self, *a):
                        return False
                return Ctx()

        result = AwardingResult(announced=True)
        result.bidders.append(Bidder(name="clock-battery-vendor", offer_value=100_000))
        result.awardees.append(Awardee(name="clock-battery-vendor", offer_value=100_000,
                                       award_value=100_000))
        await store_awarding(OneConn(), tid, result)
        assert await conn.fetchval(
            "SELECT first_seen FROM tender_award_first_seen WHERE tender_id=$1", tid) == T0
    asyncio.run(_in_rollback(body))
