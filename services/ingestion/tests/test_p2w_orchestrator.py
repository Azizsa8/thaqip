"""Tests for p2w.orchestrator — composition, the capability ladder, degradation.

Three layers:

1. Pure helpers (grid, draws, level capping, contract round-trip) with no I/O.
2. Composition against monkeypatched sub-models, which is the only practical way
   to drive every rung of the ladder and every failure branch: the real database
   contains one market and cannot be asked to produce a tier-A vendor on demand.
3. Live-database tests, skipped when the database is not reachable, that check
   the two claims which only real data can support: a thin-evidence tender comes
   back market-only at L1, and a second scenario for the same tender inserts a
   new version rather than mutating the first. These run inside a transaction
   that is always rolled back, so the live database is never left dirty.
"""
from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from typing import Any

import pytest

from thaqip_ingestion.p2w import orchestrator
from thaqip_ingestion.p2w.contracts import (
    MODEL_VERSION,
    EvidenceTier,
    ExplanationFactor,
    PredictionScope,
    PricePrediction,
    SuppressionReason,
)
from thaqip_ingestion.p2w.montecarlo import CompetitorDraw
from thaqip_ingestion.p2w.participation import ParticipationEstimate

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)

DSN = os.environ.get("THAQIP_TEST_DSN", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")


# --------------------------------------------------------------------------
# fixtures / fakes
# --------------------------------------------------------------------------


class FakeConn:
    """A connection that answers nothing. Every query path is monkeypatched."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.executed.append(sql)
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.executed.append(sql)
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.executed.append(sql)
        return 1

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(sql)
        return "UPDATE 1"


TENDER_ROW = {
    "id": 42,
    "source": "etimad",
    "reference_number": "REF-1",
    "name": "مشروع تجريبي",
    "agency_id": 5,
    "agency_name_raw": "جهة",
    "agency_canonical_name": "جهة حكومية",
    "branch_name": None,
    "activity_id": 902,
    "activity_name_raw": "تقنية معلومات",
    "tender_type_id": 1,
    "tender_type_name": "عام",
    "status_id": 1,
    "status_name": "منافسة قائمة",
    "booklet_price": 200.0,
    "published_at": datetime(2025, 10, 1, tzinfo=UTC),
    "last_enquiries_date": None,
    "last_offer_date": datetime(2025, 11, 1, tzinfo=UTC),
    "offers_opening_date": datetime(2025, 11, 2, tzinfo=UTC),
    "submitted_bids_count": 3,
    "draft_bids_count": 0,
    "external_bids_count": 0,
}


def market_prediction(*, suppressed: bool = False, p50: float = 1_000_000.0) -> PricePrediction:
    if suppressed:
        return PricePrediction.suppressed(
            tender_id=42,
            scope=PredictionScope.MARKET,
            reason=SuppressionReason.NO_COMPARABLE_TENDERS,
            evidence_tier=EvidenceTier.D,
            generated_at=AS_OF,
        )
    return PricePrediction(
        tender_id=42,
        prediction_scope=PredictionScope.MARKET,
        p10=p50 * 0.8,
        p50=p50,
        p90=p50 * 1.25,
        expected_value=p50,
        confidence_score=52,
        similarity_confidence=60,
        data_freshness_score=70,
        evidence_count=9,
        evidence_tier=EvidenceTier.C,
        feature_snapshot_id="snap-1",
        generated_at=AS_OF,
    )


def competitor_prediction(
    vendor_id: int,
    *,
    suppressed: bool = False,
    tier: EvidenceTier = EvidenceTier.B,
    p50: float = 950_000.0,
) -> PricePrediction:
    if suppressed:
        return PricePrediction.suppressed(
            tender_id=42,
            scope=PredictionScope.COMPETITOR,
            reason=SuppressionReason.INSUFFICIENT_EVIDENCE,
            subject_id=vendor_id,
            evidence_tier=EvidenceTier.C,
            generated_at=AS_OF,
        )
    return PricePrediction(
        tender_id=42,
        prediction_scope=PredictionScope.COMPETITOR,
        subject_id=vendor_id,
        p10=p50 * 0.85,
        p50=p50,
        p90=p50 * 1.18,
        confidence_score=45,
        evidence_count=11,
        evidence_tier=tier,
        generated_at=AS_OF,
    )


def participation(vendor_id: int, *, probability: float | None = 0.4) -> ParticipationEstimate:
    if probability is None:
        return ParticipationEstimate(
            vendor_id=vendor_id,
            probability=None,
            evidence_count=1,
            basis="INSUFFICIENT",
            suppression=SuppressionReason.INSUFFICIENT_EVIDENCE,
        )
    return ParticipationEstimate(
        vendor_id=vendor_id,
        probability=probability,
        evidence_count=12,
        basis="ACTIVITY",
        suppression=None,
    )


@pytest.fixture()
def wired(monkeypatch: pytest.MonkeyPatch):
    """Wire every sub-model to a controllable stub.

    Returns a mutable config dict; a test changes what it needs and calls
    ``tender_intelligence``. Assigning a callable that raises is how the
    degradation paths are exercised.
    """
    from thaqip_ingestion.p2w import competitor as competitor_mod
    from thaqip_ingestion.p2w import explain as explain_mod
    from thaqip_ingestion.p2w import market as market_mod
    from thaqip_ingestion.p2w import participation as participation_mod
    from thaqip_ingestion.p2w import similarity as similarity_mod

    config: dict[str, Any] = {
        "tender": dict(TENDER_ROW),
        "similar": [],
        "market": market_prediction(),
        "candidates": [],
        "competitor": {},
        "names": {},
        "raise_in": set(),
    }

    async def fake_load_tender(conn: Any, tender_id: int) -> dict[str, Any]:
        return dict(config["tender"])

    async def fake_similar(conn: Any, **kwargs: Any) -> list[Any]:
        if "similarity" in config["raise_in"]:
            raise RuntimeError("similarity blew up")
        return list(config["similar"])

    async def fake_market(conn: Any, **kwargs: Any) -> PricePrediction:
        if "market" in config["raise_in"]:
            raise RuntimeError("market blew up")
        return config["market"]

    async def fake_candidates(conn: Any, **kwargs: Any) -> list[ParticipationEstimate]:
        if "participation" in config["raise_in"]:
            raise RuntimeError("participation blew up")
        return list(config["candidates"])

    async def fake_competitor(conn: Any, *, vendor_id: int, **kwargs: Any) -> PricePrediction:
        if "competitor" in config["raise_in"]:
            raise RuntimeError("competitor blew up")
        return config["competitor"][vendor_id]

    async def fake_names(conn: Any, ids: Any) -> dict[int, str]:
        return dict(config["names"])

    async def fake_explanation(conn: Any, **kwargs: Any) -> dict[str, Any]:
        if "explanation" in config["raise_in"]:
            raise RuntimeError("explanation blew up")
        return {
            "observed": [],
            "derived": [],
            "predicted": [],
            "drivers": [],
            "data_quality_warnings": [],
            "suppression": None,
        }

    monkeypatch.setattr(orchestrator, "load_tender", fake_load_tender)
    monkeypatch.setattr(orchestrator, "_vendor_names", fake_names)
    monkeypatch.setattr(similarity_mod, "find_similar_tenders", fake_similar)
    monkeypatch.setattr(market_mod, "market_quantiles", fake_market)
    monkeypatch.setattr(participation_mod, "candidate_bidders", fake_candidates)
    monkeypatch.setattr(competitor_mod, "competitor_quantiles", fake_competitor)
    monkeypatch.setattr(explain_mod, "build_explanation", fake_explanation)
    return config


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def test_levels_are_ordered_and_monotone():
    assert orchestrator.LEVELS == ("L0", "L1", "L2", "L3", "L4")
    assert orchestrator._cap("L4", "L2") == "L2"
    assert orchestrator._cap("L1", "L3") == "L1"
    assert orchestrator._cap("L0", "L0") == "L0"


def test_price_grid_spans_the_market_range():
    grid = orchestrator.build_price_grid(market_prediction(p50=1_000_000.0))
    assert len(grid) == orchestrator.GRID_POINTS
    assert grid == sorted(grid)
    assert math.isclose(grid[0], orchestrator.GRID_LOW_FACTOR * 800_000.0)
    assert math.isclose(grid[-1], orchestrator.GRID_HIGH_FACTOR * 1_250_000.0)


def test_price_grid_is_empty_for_a_suppressed_market():
    assert orchestrator.build_price_grid(market_prediction(suppressed=True)) == []


def test_price_grid_never_goes_below_one_riyal():
    tiny = PricePrediction(
        tender_id=1,
        prediction_scope=PredictionScope.MARKET,
        p10=0.0,
        p50=1.0,
        p90=2.0,
        generated_at=AS_OF,
    )
    grid = orchestrator.build_price_grid(tiny)
    assert grid[0] >= 1.0
    assert grid == sorted(grid)


def test_price_grid_refuses_a_degenerate_point_count():
    assert orchestrator.build_price_grid(market_prediction(), points=1) == []


def test_draws_skip_vendors_without_a_price_band():
    draws = orchestrator.build_competitor_draws(
        [
            {
                "vendor_id": 7,
                "prediction": competitor_prediction(7, suppressed=True),
                "participation": participation(7).to_dict(),
            }
        ]
    )
    assert draws == []


def test_draws_skip_vendors_without_a_participation_probability():
    draws = orchestrator.build_competitor_draws(
        [
            {
                "vendor_id": 7,
                "prediction": competitor_prediction(7),
                "participation": participation(7, probability=None).to_dict(),
            }
        ]
    )
    assert draws == []


def test_draws_carry_the_lognormal_recovered_from_the_band():
    entry = {
        "vendor_id": 7,
        "prediction": competitor_prediction(7, p50=950_000.0),
        "participation": participation(7, probability=0.42).to_dict(),
    }
    draw = orchestrator.build_competitor_draws([entry])[0]
    assert isinstance(draw, CompetitorDraw)
    assert draw.vendor_id == 7
    assert draw.participation_p == 0.42
    assert math.isclose(draw.median_bid, 950_000.0, rel_tol=1e-9)
    assert draw.log_sigma > 0.0
    assert draw.technical_pass_p == orchestrator.DEFAULT_TECHNICAL_PASS_P


def test_draws_are_sorted_by_vendor_id_for_determinism():
    entries = [
        {
            "vendor_id": v,
            "prediction": competitor_prediction(v),
            "participation": participation(v).to_dict(),
        }
        for v in (9, 3, 6)
    ]
    assert [d.vendor_id for d in orchestrator.build_competitor_draws(entries)] == [3, 6, 9]


def test_prediction_round_trips_through_its_dict_form():
    original = market_prediction()
    original.explanation_factors = [
        ExplanationFactor(name="sample", direction="increases", weight=0.4, kind="derived")
    ]
    restored = orchestrator._prediction_from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()


def test_suppressed_prediction_round_trips_too():
    original = market_prediction(suppressed=True)
    restored = orchestrator._prediction_from_dict(original.to_dict())
    assert restored.is_suppressed
    assert restored.suppression_reason is SuppressionReason.NO_COMPARABLE_TENDERS
    assert restored.evidence_tier is EvidenceTier.D


# --------------------------------------------------------------------------
# the capability ladder
# --------------------------------------------------------------------------


async def test_suppressed_market_is_l0_facts_only(wired):
    wired["market"] = market_prediction(suppressed=True)
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["allowed_level"] == "L0"
    assert out["market"]["is_suppressed"] is True
    assert out["competitors"] == []
    assert out["suppression"]["reason"] == "NO_COMPARABLE_TENDERS"
    assert out["tender"]["name"] == "مشروع تجريبي"


async def test_market_only_is_l1(wired):
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["allowed_level"] == "L1"
    assert out["market"]["p50"] == 1_000_000.0
    assert out["suppression"] is None


async def test_suppressed_participation_stays_at_l1(wired):
    wired["candidates"] = [participation(7, probability=None)]
    wired["competitor"] = {7: competitor_prediction(7, suppressed=True)}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["allowed_level"] == "L1"
    assert len(out["competitors"]) == 1
    assert out["competitors"][0]["prediction"]["is_suppressed"] is True


async def test_participation_without_prices_is_l2(wired):
    wired["candidates"] = [participation(7)]
    wired["competitor"] = {7: competitor_prediction(7, suppressed=True)}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["allowed_level"] == "L2"


async def test_a_tier_b_competitor_band_is_l3(wired):
    wired["candidates"] = [participation(7)]
    wired["competitor"] = {7: competitor_prediction(7, tier=EvidenceTier.B)}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["allowed_level"] == "L3"


async def test_a_tier_a_competitor_band_is_l4(wired):
    wired["candidates"] = [participation(7)]
    wired["competitor"] = {7: competitor_prediction(7, tier=EvidenceTier.A)}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["allowed_level"] == "L4"


async def test_include_competitors_false_keeps_the_page_at_l1(wired):
    wired["candidates"] = [participation(7)]
    wired["competitor"] = {7: competitor_prediction(7, tier=EvidenceTier.A)}
    out = await orchestrator.tender_intelligence(
        FakeConn(), tender_id=42, include_competitors=False
    )
    assert out["allowed_level"] == "L1"
    assert out["competitors"] == []


# --------------------------------------------------------------------------
# graceful degradation
# --------------------------------------------------------------------------


async def test_market_failure_degrades_to_l0_without_raising(wired):
    wired["raise_in"] = {"market"}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["allowed_level"] == "L0"
    assert out["market"]["suppression_reason"] == "MODEL_UNAVAILABLE"
    assert out["degradations"][0]["stage"] == "market"
    assert out["suppression"]["reason"] == "MODEL_UNAVAILABLE"


async def test_similarity_failure_does_not_raise(wired):
    wired["raise_in"] = {"similarity"}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["degradations"][0]["stage"] == "similarity"
    assert out["allowed_level"] == "L0"  # capped by the similarity degradation
    assert out["similar_tenders"] == []


async def test_participation_failure_caps_at_l1(wired):
    wired["raise_in"] = {"participation"}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["allowed_level"] == "L1"
    assert out["degradations"][0]["stage"] == "participation"
    assert out["competitors"] == []


async def test_competitor_failure_caps_at_l2_and_suppresses_that_vendor(wired):
    wired["candidates"] = [participation(7)]
    wired["raise_in"] = {"competitor"}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["allowed_level"] == "L2"
    assert out["competitors"][0]["prediction"]["is_suppressed"] is True
    assert out["competitors"][0]["prediction"]["suppression_reason"] == "MODEL_UNAVAILABLE"
    assert out["degradations"][0]["stage"] == "competitor"


async def test_explanation_failure_still_returns_a_usable_payload(wired):
    wired["raise_in"] = {"explanation"}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["allowed_level"] == "L1"
    assert out["explanation"]["observed"] == []
    assert out["explanation"]["data_quality_warnings"]
    assert out["degradations"][-1]["stage"] == "explanation"


async def test_a_missing_tender_raises_rather_than_degrading():
    conn = FakeConn()
    with pytest.raises(orchestrator.TenderNotFound):
        await orchestrator.tender_intelligence(conn, tender_id=999_999)


async def test_undercut_risk_is_none_for_a_suppressed_competitor(wired):
    wired["candidates"] = [participation(7)]
    wired["competitor"] = {7: competitor_prediction(7, suppressed=True)}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    assert out["competitors"][0]["undercut_risk"] is None
    assert out["competitors"][0]["undercut_risk_reference_price"] is None


async def test_undercut_risk_is_reported_against_the_market_median(wired):
    wired["candidates"] = [participation(7)]
    wired["competitor"] = {7: competitor_prediction(7)}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    entry = out["competitors"][0]
    assert 0.0 <= entry["undercut_risk"] <= 1.0
    assert entry["undercut_risk_reference_price"] == 1_000_000.0


async def test_payload_is_json_serialisable(wired):
    """The composition root is what the API returns; it must need no adapter."""
    import json

    wired["candidates"] = [participation(7)]
    wired["competitor"] = {7: competitor_prediction(7)}
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42)
    encoded = json.dumps(out, ensure_ascii=False)
    assert json.loads(encoded)["competitors"][0]["prediction"]["p50"] == 950_000.0


def test_draws_accept_the_serialised_payload_form():
    entry = {
        "vendor_id": 7,
        "prediction": competitor_prediction(7, p50=950_000.0).to_dict(),
        "participation": participation(7, probability=0.42).to_dict(),
    }
    draw = orchestrator.build_competitor_draws([entry])[0]
    assert draw.vendor_id == 7
    assert math.isclose(draw.median_bid, 950_000.0, rel_tol=1e-9)


def test_draws_ignore_an_unreadable_prediction():
    assert orchestrator.build_competitor_draws([{"vendor_id": 7, "prediction": "nonsense"}]) == []


async def test_payload_shape_is_stable(wired):
    out = await orchestrator.tender_intelligence(FakeConn(), tender_id=42, tenant_id=3)
    assert set(out) == {
        "tender",
        "tenant_id",
        "as_of",
        "market",
        "evidence_tier",
        "allowed_level",
        "competitors",
        "similar_tenders",
        "explanation",
        "suppression",
        "degradations",
        "generated_at",
        "model_version",
    }
    assert out["tenant_id"] == 3
    assert out["model_version"] == MODEL_VERSION
    assert out["tender"]["kind"] == "observed"


# --------------------------------------------------------------------------
# live database
# --------------------------------------------------------------------------


@pytest.fixture()
async def db():
    """A live connection inside a transaction that is always rolled back."""
    asyncpg = pytest.importorskip("asyncpg")
    try:
        conn = await asyncpg.connect(DSN, timeout=5)
    except Exception as exc:  # noqa: BLE001 - any connection failure means 'skip'
        pytest.skip(f"live database unavailable: {exc}")
    transaction = conn.transaction()
    await transaction.start()
    try:
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()


async def _sample_tender_ids(conn: Any, step: int = 23) -> list[int]:
    rows = await conn.fetch("SELECT id FROM tenders ORDER BY id")
    return [int(r["id"]) for r in rows][::step]


async def test_live_thin_evidence_tender_is_market_only_at_l1(db):
    """On this dataset most tenders cannot support competitor prices.

    Scans a sample and asserts the ladder's meaning on real data: an L1 tender
    has a market range and **no** competitor price band, and an L0 tender has no
    market number at all. Skips (rather than passing vacuously) if the sample
    happens to contain no L1 tender.
    """
    seen_l1 = None
    for tender_id in await _sample_tender_ids(db):
        out = await orchestrator.tender_intelligence(db, tender_id=tender_id)
        level = out["allowed_level"]
        if level == "L0":
            assert out["market"]["is_suppressed"] is True
            assert out["market"]["suppression_reason"] is not None
        else:
            assert out["market"]["is_suppressed"] is False
            assert out["market"]["p10"] <= out["market"]["p50"] <= out["market"]["p90"]
        if level == "L1" and seen_l1 is None:
            seen_l1 = out
    if seen_l1 is None:  # pragma: no cover - data dependent
        pytest.skip("no L1 tender in the sampled ids")

    assert seen_l1["market"]["p50"] > 0
    assert seen_l1["evidence_tier"] in {"A", "B", "C"}
    priced = [
        c for c in seen_l1["competitors"] if not c["prediction"]["is_suppressed"]
    ]
    assert priced == [], "an L1 tender must not expose any competitor price band"
    assert seen_l1["explanation"]["predicted"], "a market range must appear in 'predicted'"


async def test_live_explanation_keeps_predictions_out_of_observed(db):
    """The FR-012 hard rule, checked against real rows rather than fakes."""
    checked = 0
    for tender_id in await _sample_tender_ids(db, step=97):
        out = await orchestrator.tender_intelligence(db, tender_id=tender_id)
        explanation = out["explanation"]
        predicted_values = {
            value
            for entry in explanation["predicted"]
            for value in (entry["p10"], entry["p50"], entry["p90"])
        }
        for entry in explanation["observed"]:
            assert entry["kind"] == "observed"
            assert entry["source_table"] in {"awards", "vendors", "tenders", "offers", "agencies"}
            assert isinstance(entry["source_id"], int)
            assert entry["value"] not in predicted_values
            checked += 1
    assert checked > 0, "expected at least one observed fact across the sample"


async def test_live_scenario_versioning_inserts_a_new_row(db):
    """US-03: rerunning a scenario versions it; it never mutates the old row."""
    tender_id = int(await db.fetchval("SELECT id FROM tenders ORDER BY id LIMIT 1"))
    before = await db.fetchval(
        "SELECT count(*) FROM user_bid_scenarios WHERE tenant_id = 1 AND tender_id = $1",
        tender_id,
    )

    first = await orchestrator.scenario_simulation(
        db,
        tender_id=tender_id,
        tenant_id=1,
        estimated_cost=250_000.0,
        min_margin_pct=12.0,
        seed=4242,
        iterations=200,
    )
    second = await orchestrator.scenario_simulation(
        db,
        tender_id=tender_id,
        tenant_id=1,
        estimated_cost=260_000.0,
        min_margin_pct=12.0,
        seed=4242,
        iterations=200,
    )

    assert first["scenario"]["id"] is not None
    assert second["scenario"]["version"] == first["scenario"]["version"] + 1
    assert second["scenario"]["superseded_id"] == first["scenario"]["id"]

    rows = await db.fetch(
        "SELECT id, version, estimated_cost, superseded_by FROM user_bid_scenarios "
        "WHERE tenant_id = 1 AND tender_id = $1 ORDER BY version",
        tender_id,
    )
    assert len(rows) == before + 2
    old, new = dict(rows[-2]), dict(rows[-1])
    # The old row keeps its own inputs — versioning, not overwriting.
    assert float(old["estimated_cost"]) == 250_000.0
    assert float(new["estimated_cost"]) == 260_000.0
    assert old["superseded_by"] == new["id"]
    assert new["superseded_by"] is None


async def test_live_scenario_persists_a_user_optimizer_prediction(db):
    tender_id = int(await db.fetchval("SELECT id FROM tenders ORDER BY id LIMIT 1"))
    out = await orchestrator.scenario_simulation(
        db,
        tender_id=tender_id,
        tenant_id=1,
        estimated_cost=250_000.0,
        min_margin_pct=12.0,
        seed=99,
        iterations=200,
    )
    assert out["prediction_id"] is not None
    row = dict(
        await db.fetchrow(
            "SELECT prediction_scope, model_version, seed, suppression_reason, p50 "
            "FROM price_predictions WHERE id = $1",
            out["prediction_id"],
        )
    )
    assert row["prediction_scope"] == "USER_OPTIMIZER"
    assert row["model_version"] == MODEL_VERSION
    assert row["seed"] == 99
    # Whatever the outcome, the DB row and the returned contract agree about it.
    assert (row["suppression_reason"] is not None) == out["prediction"]["is_suppressed"]
    assert (row["p50"] is None) == (out["prediction"]["p50"] is None)


async def test_live_scenario_is_reproducible_from_seed(db):
    """Determinism (house rule 7): same inputs + seed => identical curve."""
    tender_id = None
    for candidate in await _sample_tender_ids(db, step=23):
        out = await orchestrator.tender_intelligence(db, tender_id=candidate)
        if out["allowed_level"] in {"L3", "L4"}:
            tender_id = candidate
            break
    if tender_id is None:  # pragma: no cover - data dependent
        pytest.skip("no simulation-capable tender in the sampled ids")

    kwargs = dict(
        tender_id=tender_id,
        tenant_id=1,
        estimated_cost=50_000.0,
        min_margin_pct=10.0,
        seed=7,
        iterations=400,
    )
    first = await orchestrator.scenario_simulation(db, **kwargs)
    second = await orchestrator.scenario_simulation(db, **kwargs)
    assert first["curve"] == second["curve"]
    assert first["optimizer"] == second["optimizer"]
    assert first["feature_snapshot_id"] == second["feature_snapshot_id"]
    assert first["prediction"]["p50"] == second["prediction"]["p50"]


async def test_live_scenario_suppresses_when_the_market_is_suppressed(db):
    """A tender with no supportable market range yields no curve and says why."""
    target = None
    for candidate in await _sample_tender_ids(db, step=23):
        out = await orchestrator.tender_intelligence(db, tender_id=candidate)
        if out["allowed_level"] == "L0":
            target = candidate
            break
    if target is None:  # pragma: no cover - data dependent
        pytest.skip("no L0 tender in the sampled ids")

    out = await orchestrator.scenario_simulation(
        db,
        tender_id=target,
        tenant_id=1,
        estimated_cost=100_000.0,
        min_margin_pct=15.0,
        proposed_bid=120_000.0,
        seed=11,
        iterations=200,
    )
    assert out["curve"] == []
    assert out["price_grid"] == []
    assert out["optimizer"] is None
    assert out["prediction"]["is_suppressed"] is True
    assert out["suppression"]["scope"] == "USER_OPTIMIZER"
    assert out["proposed_bid_simulation"]["win_probability"] is None
    # The user's inputs are still recorded even though the engine cannot answer.
    assert out["scenario"]["id"] is not None
