"""Tests for p2w.market — deterministic, no network, no database writes.

The weighted quantile carries most of the weight here: it is the numeric heart
of the product, so it is checked against hand-computed values, against a plain
(unweighted) reference implementation, and on its edge cases.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest

from thaqip_ingestion.p2w import evidence as ev
from thaqip_ingestion.p2w import market
from thaqip_ingestion.p2w import similarity as sim
from thaqip_ingestion.p2w.contracts import (
    MODEL_VERSION,
    EvidenceTier,
    PredictionScope,
    SuppressionReason,
)

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# helpers / fakes
# --------------------------------------------------------------------------


class FakeConn:
    """Asyncpg-shaped stand-in. market.py must never actually query it here."""

    def __init__(self) -> None:
        self.fetch_calls: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append(args)
        return []


def plain_quantile(values: list[float], q: float) -> float:
    """Reference unweighted linear-interpolation quantile (numpy's default).

    Deliberately written from the definition rather than reused from the module
    under test: position = q * (n - 1), interpolate between neighbours.
    """
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (position - low) * (ordered[high] - ordered[low])


def make_similar(
    tender_id: int,
    award_value: float | None = 1_000_000.0,
    *,
    score: float = 0.8,
    age_days: float = 100.0,
    exclusion: str | None = None,
    name: str = "عقد صيانة",
) -> sim.SimilarTender:
    return sim.SimilarTender(
        tender_id=tender_id,
        name=name,
        agency="جهة حكومية",
        award_value=award_value,
        bidder_count=4,
        total_score=score,
        components={},
        exclusion_reason=exclusion,
        age_days=age_days,
        notes=(),
    )


def make_evidence(
    *,
    tier: EvidenceTier = EvidenceTier.C,
    comparable_count: int = 9,
    median_age_days: float = 120.0,
    freshness: int = 84,
    similarity_confidence: int = 70,
    suppression: SuppressionReason | None = None,
) -> ev.MarketEvidence:
    return ev.MarketEvidence(
        comparable_count=comparable_count,
        median_age_days=median_age_days,
        freshness=freshness,
        similarity_confidence=similarity_confidence,
        tier=tier,
        suppression=suppression,
    )


def patch_evidence(monkeypatch: pytest.MonkeyPatch, evidence: ev.MarketEvidence) -> None:
    async def _fake(conn: Any, *, tender: Any, as_of: Any = None) -> ev.MarketEvidence:
        return evidence

    monkeypatch.setattr(ev, "market_evidence", _fake)


TENDER = {
    "id": 42,
    "name": "تشغيل وصيانة مبانٍ",
    "activity_id": 111,
    "agency_id": 7,
    "offers_opening_date": AS_OF,
}


# --------------------------------------------------------------------------
# weighted_quantile — hand-computed values
# --------------------------------------------------------------------------


def test_weighted_quantile_single_point_returns_that_point() -> None:
    for q in (0.0, 0.1, 0.5, 0.9, 1.0):
        assert market.weighted_quantile([7.5], [3.0], q) == 7.5


def test_weighted_quantile_two_points_is_linear_between_them() -> None:
    # Equal weights on [10, 20]: positions are 0 and 1, so q maps straight through.
    assert market.weighted_quantile([10.0, 20.0], [1.0, 1.0], 0.0) == 10.0
    assert market.weighted_quantile([10.0, 20.0], [1.0, 1.0], 0.5) == 15.0
    assert market.weighted_quantile([10.0, 20.0], [1.0, 1.0], 0.1) == pytest.approx(11.0)
    assert market.weighted_quantile([10.0, 20.0], [1.0, 1.0], 1.0) == 20.0


def test_weighted_quantile_hand_computed_four_points() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    weights = [1.0, 1.0, 1.0, 1.0]
    # positions = (i-1)/3 -> 0, 1/3, 2/3, 1
    # q=0.1 -> between v1 and v2 at 0.1/(1/3)=0.3 -> 1.3
    assert market.weighted_quantile(values, weights, 0.10) == pytest.approx(1.3)
    assert market.weighted_quantile(values, weights, 0.50) == pytest.approx(2.5)
    assert market.weighted_quantile(values, weights, 0.90) == pytest.approx(3.7)


def test_weighted_quantile_hand_computed_unequal_weights() -> None:
    # values 10, 20, 30 with weights 1, 2, 1.
    # C = [1, 3, 4]; S = 4; offset = 0.5; denom = 4 - (1+1)/2 = 3
    # p1 = (1 - 0.5 - 0.5)/3 = 0
    # p2 = (3 - 1.0 - 0.5)/3 = 0.5
    # p3 = (4 - 0.5 - 0.5)/3 = 1
    values, weights = [10.0, 20.0, 30.0], [1.0, 2.0, 1.0]
    assert market.weighted_quantile(values, weights, 0.5) == pytest.approx(20.0)
    # q=0.25 -> halfway between p1=0 and p2=0.5 -> 15
    assert market.weighted_quantile(values, weights, 0.25) == pytest.approx(15.0)
    # q=0.75 -> halfway between p2=0.5 and p3=1 -> 25
    assert market.weighted_quantile(values, weights, 0.75) == pytest.approx(25.0)


@pytest.mark.parametrize("q", [0.0, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0])
def test_weighted_quantile_equal_weights_equals_plain_quantile(q: float) -> None:
    values = [3.0, 1.0, 4.0, 1.5, 9.0, 2.6, 5.0]
    for w in (1.0, 7.25, 0.001):  # magnitude must not matter, only equality
        weights = [w] * len(values)
        assert market.weighted_quantile(values, weights, q) == pytest.approx(
            plain_quantile(values, q)
        )


def test_weighted_quantile_ignores_zero_weight_points() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 1000.0]
    with_zero = market.weighted_quantile(values, [1.0, 1.0, 1.0, 1.0, 0.0], 0.5)
    without = market.weighted_quantile(values[:4], [1.0] * 4, 0.5)
    assert with_zero == pytest.approx(without)


def test_weighted_quantile_extreme_weight_concentration_pulls_toward_heavy_point() -> None:
    values = [10.0, 20.0, 30.0]
    heavy_middle = market.weighted_quantile(values, [1.0, 1_000_000.0, 1.0], 0.5)
    assert heavy_middle == pytest.approx(20.0, abs=1e-3)
    # A dominant low point drags every quantile down relative to equal weights.
    equal = [market.weighted_quantile(values, [1.0, 1.0, 1.0], q) for q in (0.1, 0.5, 0.9)]
    heavy_low = [
        market.weighted_quantile(values, [1_000_000.0, 1.0, 1.0], q) for q in (0.1, 0.5, 0.9)
    ]
    assert all(h <= e for h, e in zip(heavy_low, equal))
    assert heavy_low[1] < equal[1]


def test_weighted_quantile_never_leaves_the_observed_range() -> None:
    values = [120.0, 340.0, 90.0, 700.0]
    weights = [0.3, 5.0, 0.2, 1.0]
    for i in range(101):
        q = i / 100.0
        out = market.weighted_quantile(values, weights, q)
        assert min(values) <= out <= max(values)


def test_weighted_quantile_is_monotone_in_q() -> None:
    values = [5.0, 11.0, 12.5, 40.0, 41.0]
    weights = [0.2, 1.0, 0.7, 3.0, 0.1]
    previous = -math.inf
    for i in range(101):
        out = market.weighted_quantile(values, weights, i / 100.0)
        assert out >= previous - 1e-12
        previous = out


def test_weighted_quantile_is_symmetric_under_mirroring() -> None:
    values = [1.0, 4.0, 9.0, 16.0]
    weights = [1.0, 3.0, 0.5, 2.0]
    mirrored_values = [-v for v in values]
    for q in (0.1, 0.5, 0.9):
        direct = market.weighted_quantile(values, weights, q)
        mirrored = -market.weighted_quantile(mirrored_values, weights, 1.0 - q)
        assert direct == pytest.approx(mirrored)


def test_weighted_quantile_all_identical_values() -> None:
    assert market.weighted_quantile([7.0, 7.0, 7.0], [1.0, 5.0, 2.0], 0.9) == 7.0


@pytest.mark.parametrize(
    "values,weights,q",
    [
        ([], [], 0.5),
        ([1.0, 2.0], [1.0], 0.5),
        ([1.0, 2.0], [1.0, 1.0], -0.01),
        ([1.0, 2.0], [1.0, 1.0], 1.01),
        ([1.0, 2.0], [1.0, 1.0], float("nan")),
        ([1.0, 2.0], [1.0, -1.0], 0.5),
        ([1.0, 2.0], [0.0, 0.0], 0.5),
        ([1.0, float("inf")], [1.0, 1.0], 0.5),
        ([1.0, 2.0], [1.0, float("nan")], 0.5),
    ],
)
def test_weighted_quantile_rejects_bad_input(
    values: list[float], weights: list[float], q: float
) -> None:
    with pytest.raises(ValueError):
        market.weighted_quantile(values, weights, q)


# --------------------------------------------------------------------------
# weights and time adjustment
# --------------------------------------------------------------------------


def test_recency_weight_half_life() -> None:
    assert market.recency_weight(0.0) == 1.0
    assert market.recency_weight(-50.0) == 1.0  # future-dated row is not extra fresh
    assert market.recency_weight(market.RECENCY_HALF_LIFE_DAYS) == pytest.approx(0.5)
    assert market.recency_weight(2 * market.RECENCY_HALF_LIFE_DAYS) == pytest.approx(0.25)
    assert market.recency_weight(10_000.0) > 0.0


def test_inflation_factor_is_compound_and_documented_default() -> None:
    assert market.DEFAULT_ANNUAL_INFLATION == 0.02
    assert market.inflation_factor(0.0) == 1.0
    assert market.inflation_factor(-5.0) == 1.0
    assert market.inflation_factor(market.DAYS_PER_YEAR) == pytest.approx(1.02)
    assert market.inflation_factor(2 * market.DAYS_PER_YEAR) == pytest.approx(1.02**2)
    assert market.inflation_factor(market.DAYS_PER_YEAR, 0.0) == 1.0


def test_inflation_factor_rejects_impossible_rate() -> None:
    with pytest.raises(ValueError):
        market.inflation_factor(100.0, -1.0)


# --------------------------------------------------------------------------
# build_sample
# --------------------------------------------------------------------------


def test_build_sample_adjusts_value_and_weights_by_similarity_and_recency() -> None:
    item = make_similar(5, 1_000_000.0, score=0.5, age_days=365.0)
    (row,) = market.build_sample([item])
    assert row.observed_value == 1_000_000.0
    assert row.adjusted_value == pytest.approx(
        1_000_000.0 * 1.02 ** (365.0 / market.DAYS_PER_YEAR)
    )
    assert row.recency_weight == pytest.approx(0.5)
    assert row.weight == pytest.approx(0.25)
    assert row.to_dict()["tender_id"] == 5


def test_build_sample_drops_unusable_rows() -> None:
    rows = market.build_sample(
        [
            make_similar(1, None),                       # no award value
            make_similar(2, 1_000.0, exclusion="scale"),  # excluded by retrieval
            make_similar(3, 0.0),                        # implausible award value
            make_similar(4, 1_000.0, score=0.01),        # below similarity floor
            make_similar(5, 1_000.0, age_days=float("nan")),
            make_similar(6, 1_000.0),                    # keeper
        ]
    )
    assert [r.tender_id for r in rows] == [6]


def test_build_sample_accepts_mappings_and_sorts_by_tender_id() -> None:
    rows = market.build_sample(
        [
            {
                "tender_id": 9,
                "name": "ب",
                "award_value": 200.0,
                "total_score": 0.9,
                "age_days": 10.0,
                "exclusion_reason": None,
            },
            {
                "tender_id": 2,
                "name": "أ",
                "award_value": 100.0,
                "total_score": 0.9,
                "age_days": 10.0,
                "exclusion_reason": None,
            },
        ]
    )
    assert [r.tender_id for r in rows] == [2, 9]


# --------------------------------------------------------------------------
# feature_snapshot_id determinism
# --------------------------------------------------------------------------


def test_feature_snapshot_id_is_stable_and_order_independent() -> None:
    a = market.feature_snapshot_id([3, 1, 2], AS_OF)
    b = market.feature_snapshot_id([1, 2, 3], AS_OF)
    c = market.feature_snapshot_id([1, 2, 3], datetime(2026, 1, 1, 23, 59, tzinfo=UTC))
    assert a == b == c
    assert len(a) == 16
    assert int(a, 16) >= 0  # it is hex


def test_feature_snapshot_id_changes_with_inputs() -> None:
    base = market.feature_snapshot_id([1, 2, 3], AS_OF)
    assert base != market.feature_snapshot_id([1, 2, 4], AS_OF)
    assert base != market.feature_snapshot_id([1, 2], AS_OF)
    assert base != market.feature_snapshot_id([1, 2, 3], datetime(2026, 1, 2, tzinfo=UTC))
    assert base != market.feature_snapshot_id([1, 2, 3], AS_OF, model_version="p2w-9.9.9")


# --------------------------------------------------------------------------
# fraction_at_or_below
# --------------------------------------------------------------------------


def test_fraction_at_or_below_counts_inclusively() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert market.fraction_at_or_below(values, 5.0) == 0.0
    assert market.fraction_at_or_below(values, 20.0) == 0.5
    assert market.fraction_at_or_below(values, 40.0) == 1.0
    with pytest.raises(ValueError):
        market.fraction_at_or_below([], 1.0)


# --------------------------------------------------------------------------
# market_quantiles
# --------------------------------------------------------------------------


async def test_market_quantiles_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_evidence(monkeypatch, make_evidence())
    similar = [
        make_similar(i, 800_000.0 + 100_000.0 * i, score=0.6 + i / 100.0, age_days=30.0 * i)
        for i in range(1, 8)
    ]
    out = await market.market_quantiles(FakeConn(), tender=TENDER, as_of=AS_OF, similar=similar)

    assert out.suppression_reason is None
    assert out.prediction_scope is PredictionScope.MARKET
    assert out.subject_id is None
    assert out.p10 <= out.p50 <= out.p90
    assert out.expected_value == out.p50
    assert out.evidence_count == len(similar)
    assert out.evidence_tier is EvidenceTier.C
    assert out.model_version == MODEL_VERSION
    assert out.similarity_confidence == 70
    assert out.data_freshness_score == 84
    assert 0 <= out.confidence_score <= 100
    assert out.win_probability is None  # market scope makes no win claim
    assert out.seed is None
    assert market.MIN_EXPLANATION_FACTORS <= len(out.explanation_factors)
    assert len(out.explanation_factors) <= market.MAX_EXPLANATION_FACTORS
    assert out.feature_snapshot_id == market.feature_snapshot_id(
        [s.tender_id for s in similar], AS_OF
    )


async def test_market_quantiles_quantiles_match_direct_computation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_evidence(monkeypatch, make_evidence())
    similar = [
        make_similar(1, 1_000_000.0, score=0.9, age_days=0.0),
        make_similar(2, 2_000_000.0, score=0.9, age_days=0.0),
        make_similar(3, 3_000_000.0, score=0.9, age_days=0.0),
    ]
    out = await market.market_quantiles(FakeConn(), tender=TENDER, as_of=AS_OF, similar=similar)
    # age 0 -> no inflation adjustment, equal weights -> plain quantiles.
    values = [1_000_000.0, 2_000_000.0, 3_000_000.0]
    assert out.p10 == pytest.approx(plain_quantile(values, 0.1))
    assert out.p50 == pytest.approx(plain_quantile(values, 0.5))
    assert out.p90 == pytest.approx(plain_quantile(values, 0.9))


async def test_market_quantiles_explanation_factors_are_typed_and_cite_tenders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_evidence(monkeypatch, make_evidence())
    similar = [make_similar(i, 1_000_000.0 * i, score=0.9, age_days=40.0) for i in range(1, 6)]
    out = await market.market_quantiles(FakeConn(), tender=TENDER, as_of=AS_OF, similar=similar)

    names = [f.name for f in out.explanation_factors]
    assert "sample_size" in names
    assert "time_adjustment" in names
    assert "recency_weighting" in names
    kinds = {f.kind for f in out.explanation_factors}
    assert kinds <= {"observed", "derived"}  # nothing in a market range is 'predicted' input
    assert "observed" in kinds and "derived" in kinds
    refs = [f.evidence_ref for f in out.explanation_factors if f.evidence_ref]
    assert refs and all(r.startswith("tender:") for r in refs)
    time_factor = next(f for f in out.explanation_factors if f.name == "time_adjustment")
    assert time_factor.kind == "derived"  # the inflation index is an assumption
    assert time_factor.weight > 0.0


async def test_market_quantiles_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_evidence(monkeypatch, make_evidence())
    similar = [make_similar(i, 500_000.0 * i, score=0.5 + i / 50.0, age_days=17.0 * i)
               for i in range(1, 10)]
    first = await market.market_quantiles(FakeConn(), tender=TENDER, as_of=AS_OF, similar=similar)
    second = await market.market_quantiles(
        FakeConn(), tender=TENDER, as_of=AS_OF, similar=list(reversed(similar))
    )
    a, b = first.to_dict(), second.to_dict()
    a.pop("generated_at"), b.pop("generated_at")
    assert a == b


async def test_market_quantiles_time_adjustment_moves_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_evidence(monkeypatch, make_evidence())
    similar = [make_similar(i, 1_000_000.0, score=0.9, age_days=730.0) for i in range(1, 6)]
    adjusted = await market.market_quantiles(
        FakeConn(), tender=TENDER, as_of=AS_OF, similar=similar
    )
    unadjusted = await market.market_quantiles(
        FakeConn(), tender=TENDER, as_of=AS_OF, similar=similar, annual_rate=0.0
    )
    assert unadjusted.p50 == pytest.approx(1_000_000.0)
    assert adjusted.p50 == pytest.approx(1_000_000.0 * 1.02 ** (730.0 / market.DAYS_PER_YEAR))
    assert adjusted.p50 > unadjusted.p50


async def test_market_quantiles_suppresses_on_tier_d(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_evidence(
        monkeypatch,
        make_evidence(
            tier=EvidenceTier.D,
            comparable_count=1,
            suppression=SuppressionReason.INSUFFICIENT_EVIDENCE,
        ),
    )
    out = await market.market_quantiles(
        FakeConn(), tender=TENDER, as_of=AS_OF, similar=[make_similar(1)]
    )
    assert out.is_suppressed
    assert out.suppression_reason is SuppressionReason.INSUFFICIENT_EVIDENCE
    assert out.p10 is None and out.p50 is None and out.p90 is None
    assert out.win_probability is None
    assert out.quantiles is None
    assert out.evidence_tier is EvidenceTier.D
    assert out.evidence_count == 1  # the count is still an observed fact


async def test_market_quantiles_suppresses_when_sample_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_evidence(monkeypatch, make_evidence())
    # Evidence counted awarded comparables, but retrieval scored none of them usable.
    out = await market.market_quantiles(
        FakeConn(),
        tender=TENDER,
        as_of=AS_OF,
        similar=[make_similar(1, None), make_similar(2, 10.0, score=0.01)],
    )
    assert out.is_suppressed
    assert out.suppression_reason is SuppressionReason.NO_COMPARABLE_TENDERS
    assert out.p50 is None


async def test_market_quantiles_widens_the_band_on_tier_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    similar = [make_similar(i, 1_000_000.0 * i, score=0.9, age_days=0.0) for i in range(1, 6)]
    patch_evidence(monkeypatch, make_evidence(tier=EvidenceTier.C))
    tight = await market.market_quantiles(FakeConn(), tender=TENDER, as_of=AS_OF, similar=similar)
    patch_evidence(monkeypatch, make_evidence(tier=EvidenceTier.B))
    wide = await market.market_quantiles(FakeConn(), tender=TENDER, as_of=AS_OF, similar=similar)

    assert wide.p50 == pytest.approx(tight.p50)
    assert wide.p10 < tight.p10 and wide.p90 > tight.p90
    assert wide.p10 <= wide.p50 <= wide.p90
    assert wide.p10 >= 0.0
    assert any(f.name == "tier_b_widening" for f in wide.explanation_factors)
    assert len(wide.explanation_factors) <= market.MAX_EXPLANATION_FACTORS


async def test_market_quantiles_uses_retrieval_when_similar_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_evidence(monkeypatch, make_evidence())
    seen: dict[str, Any] = {}

    async def fake_find(conn: Any, *, tender: Any, as_of: Any = None, **kw: Any) -> list[Any]:
        seen["as_of"] = as_of
        seen["tender_id"] = tender["id"]
        return [make_similar(i, 1_000_000.0 * i, score=0.7, age_days=50.0) for i in range(1, 5)]

    monkeypatch.setattr(sim, "find_similar_tenders", fake_find)
    out = await market.market_quantiles(FakeConn(), tender=TENDER, as_of=AS_OF)
    assert seen == {"as_of": AS_OF, "tender_id": 42}  # as_of is passed through, not re-derived
    assert out.evidence_count == 4


async def test_market_quantiles_uses_one_shared_cutoff_for_grading_and_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grading and retrieval must never run against two different horizons."""
    seen: dict[str, Any] = {}

    async def fake_evidence(conn: Any, *, tender: Any, as_of: Any = None) -> ev.MarketEvidence:
        seen["evidence_as_of"] = as_of
        return make_evidence()

    async def fake_find(conn: Any, *, tender: Any, as_of: Any = None, **kw: Any) -> list[Any]:
        seen["retrieval_as_of"] = as_of
        return [make_similar(i, 1_000_000.0 * i, score=0.7, age_days=50.0) for i in range(1, 5)]

    monkeypatch.setattr(ev, "market_evidence", fake_evidence)
    monkeypatch.setattr(sim, "find_similar_tenders", fake_find)

    # No offers_opening_date: the two modules derive different defaults, and the
    # earlier (stricter) one must win for both.
    tender = {"id": 42, "name": "x", "activity_id": 111, "published_at": AS_OF}
    await market.market_quantiles(FakeConn(), tender=tender)
    assert seen["evidence_as_of"] == seen["retrieval_as_of"] == AS_OF


def test_resolve_cutoff_prefers_the_stricter_derivation() -> None:
    explicit = datetime(2025, 6, 1, tzinfo=UTC)
    published = datetime(2020, 1, 1, tzinfo=UTC)
    tender = {"id": 1, "offers_opening_date": AS_OF, "published_at": published}
    assert market._resolve_cutoff(tender, explicit) == explicit  # explicit wins outright
    assert market._resolve_cutoff(tender, None) == AS_OF  # both derive the opening date
    # Only published_at: evidence would fall back to now(), similarity to
    # published_at — the earlier, stricter one must win.
    assert market._resolve_cutoff({"id": 1, "published_at": published}, None) == published


async def test_market_quantiles_output_is_json_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    patch_evidence(monkeypatch, make_evidence())
    similar = [make_similar(i, 900_000.0 * i, score=0.8, age_days=20.0) for i in range(1, 6)]
    out = await market.market_quantiles(FakeConn(), tender=TENDER, as_of=AS_OF, similar=similar)
    payload = json.loads(json.dumps(out.to_dict()))
    assert payload["is_suppressed"] is False
    assert payload["prediction_scope"] == "MARKET"


# --------------------------------------------------------------------------
# market_curve
# --------------------------------------------------------------------------


async def test_market_curve_is_an_observed_non_decreasing_step_function() -> None:
    similar = [
        make_similar(1, 1_000_000.0, score=0.9, age_days=400.0),
        make_similar(2, 2_000_000.0, score=0.9, age_days=400.0),
        make_similar(3, 3_000_000.0, score=0.9, age_days=400.0),
        make_similar(4, 4_000_000.0, score=0.9, age_days=400.0),
    ]
    grid = [500_000.0, 1_000_000.0, 2_500_000.0, 4_000_000.0, 9_000_000.0]
    curve = await market.market_curve(
        FakeConn(), tender=TENDER, as_of=AS_OF, grid=grid, similar=similar
    )
    fractions = [point["fraction_at_or_below"] for point in curve]
    assert [point["price"] for point in curve] == grid
    assert fractions == [0.0, 0.25, 0.5, 1.0, 1.0]
    assert all(point["kind"] == "observed" for point in curve)
    assert all(point["sample_size"] == 4 for point in curve)
    # 'observed' must mean observed: the inflation adjustment may not leak in.
    assert curve[1]["fraction_at_or_below"] == 0.25


async def test_market_curve_returns_empty_without_comparables() -> None:
    curve = await market.market_curve(
        FakeConn(), tender=TENDER, as_of=AS_OF, grid=[1.0, 2.0], similar=[]
    )
    assert curve == []
