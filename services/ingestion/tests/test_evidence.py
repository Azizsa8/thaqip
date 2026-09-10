"""Tests for the P2W evidence gate.

Everything here is deterministic: fixed timestamps, no sleeps, no network, and
the two DB-touching functions are exercised against a fake connection that
returns canned rows. One optional test talks to the live database read-only and
skips itself when that database is not reachable.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from thaqip_ingestion.p2w.contracts import EvidenceTier, SuppressionReason
from thaqip_ingestion.p2w.evidence import (
    CONFIDENCE_WIDTH_RATIO_CAP,
    HALF_LIFE_DAYS,
    HARD_LOW_SIMILARITY_FLOOR,
    HARD_STALE_FLOOR_FRESHNESS,
    SIM_AGENCY_ONLY,
    TIER_A_MIN_COMPARABLES,
    TIER_A_MIN_EFFECTIVE_N,
    TIER_A_MIN_FRESHNESS,
    TIER_A_MIN_SIMILARITY,
    TIER_B_MIN_COMPARABLES,
    TIER_B_MIN_EFFECTIVE_N,
    TIER_C_MIN_COMPARABLES,
    CompetitorEvidence,
    MarketEvidence,
    classify_tier,
    competitor_evidence,
    confidence_score,
    decayed_count,
    market_evidence,
    median,
    resolve_as_of,
)

AS_OF = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

# Values that clear both hard gates, so a test aimed at one dimension is not
# silently answered by another.
SAFE_SIM = 70
SAFE_FRESH = 80


# --------------------------------------------------------------------------
# Fake connection
# --------------------------------------------------------------------------

class FakeConn:
    """Returns a canned row list per call, and records the queries it saw."""

    def __init__(self, *responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.calls.append((query, args))
        if not self._responses:
            return []
        return self._responses.pop(0)


def award_row(age_days: float, *, same_agency: bool = False, same_activity: bool = True) -> dict:
    return {
        "knowable_at": AS_OF - timedelta(days=age_days),
        "same_agency": same_agency,
        "same_activity": same_activity,
    }


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_median_empty_is_zero():
    assert median([]) == 0.0


def test_median_odd_and_even():
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([4.0, 1.0, 3.0, 2.0]) == 2.5


def test_decayed_count_half_life():
    assert decayed_count([0.0]) == pytest.approx(1.0)
    assert decayed_count([HALF_LIFE_DAYS]) == pytest.approx(0.5)
    assert decayed_count([2 * HALF_LIFE_DAYS]) == pytest.approx(0.25)
    assert decayed_count([0.0, HALF_LIFE_DAYS]) == pytest.approx(1.5)


def test_decayed_count_future_dates_clamp_to_one():
    """Clock skew must not manufacture more than one observation per row."""
    assert decayed_count([-500.0]) == pytest.approx(1.0)


def test_decayed_count_is_never_more_than_raw_count():
    ages = [0.0, 10.0, 400.0, 1200.0]
    assert decayed_count(ages) <= len(ages)


def test_decayed_count_rejects_nonpositive_half_life():
    with pytest.raises(ValueError):
        decayed_count([1.0], half_life_days=0.0)


def test_decayed_count_monotone_in_recency():
    """Younger observations must never count for less than older ones."""
    previous = 0.0
    for age in range(2000, -1, -100):
        value = decayed_count([float(age)])
        assert value >= previous
        previous = value


def test_resolve_as_of_prefers_explicit():
    explicit = datetime(2020, 1, 1, tzinfo=UTC)
    assert resolve_as_of({"offers_opening_date": AS_OF}, explicit) == explicit


def test_resolve_as_of_falls_back_through_tender_dates():
    opening = datetime(2026, 5, 1, tzinfo=UTC)
    last_offer = datetime(2026, 4, 1, tzinfo=UTC)
    assert resolve_as_of({"offers_opening_date": opening, "last_offer_date": last_offer}) == opening
    assert resolve_as_of({"last_offer_date": last_offer}) == last_offer


def test_resolve_as_of_naive_timestamp_is_treated_as_utc():
    naive = datetime(2026, 5, 1, 9, 0)  # noqa: DTZ001 - the naive input is the point of this test
    assert resolve_as_of({"offers_opening_date": naive}) == naive.replace(tzinfo=UTC)


def test_resolve_as_of_defaults_to_now_when_tender_has_no_dates():
    before = datetime.now(UTC)
    resolved = resolve_as_of({})
    assert before <= resolved <= datetime.now(UTC)


# --------------------------------------------------------------------------
# classify_tier — hard gates
# --------------------------------------------------------------------------

def test_zero_comparables_is_tier_d_no_comparable_tenders():
    tier, reason = classify_tier(
        comparable_count=0,
        effective_competitor_n=50.0,
        similarity_confidence=100,
        freshness=100,
    )
    assert tier is EvidenceTier.D
    assert reason is SuppressionReason.NO_COMPARABLE_TENDERS


def test_stale_gate_at_and_below_floor():
    kwargs = dict(
        comparable_count=200, effective_competitor_n=50.0, similarity_confidence=100
    )
    tier, reason = classify_tier(freshness=HARD_STALE_FLOOR_FRESHNESS - 1, **kwargs)
    assert (tier, reason) == (EvidenceTier.D, SuppressionReason.STALE_DATA)
    # Exactly at the floor the gate does not fire.
    tier, reason = classify_tier(freshness=HARD_STALE_FLOOR_FRESHNESS, **kwargs)
    assert tier is not EvidenceTier.D


def test_low_similarity_gate_at_and_below_floor():
    kwargs = dict(comparable_count=200, effective_competitor_n=50.0, freshness=100)
    tier, reason = classify_tier(similarity_confidence=HARD_LOW_SIMILARITY_FLOOR - 1, **kwargs)
    assert (tier, reason) == (EvidenceTier.D, SuppressionReason.LOW_SIMILARITY)
    tier, reason = classify_tier(similarity_confidence=HARD_LOW_SIMILARITY_FLOOR, **kwargs)
    assert tier is not EvidenceTier.D


def test_no_comparables_gate_wins_over_stale_gate():
    _tier, reason = classify_tier(
        comparable_count=0, effective_competitor_n=0.0, similarity_confidence=0, freshness=0
    )
    assert reason is SuppressionReason.NO_COMPARABLE_TENDERS


@pytest.mark.parametrize("bad", [-1, 101])
@pytest.mark.parametrize("field", ["similarity_confidence", "freshness"])
def test_classify_tier_rejects_out_of_range_scores(field, bad):
    kwargs = dict(
        comparable_count=10,
        effective_competitor_n=1.0,
        similarity_confidence=SAFE_SIM,
        freshness=SAFE_FRESH,
    )
    kwargs[field] = bad
    with pytest.raises(ValueError):
        classify_tier(**kwargs)


def test_classify_tier_rejects_negative_counts():
    with pytest.raises(ValueError):
        classify_tier(
            comparable_count=-1,
            effective_competitor_n=1.0,
            similarity_confidence=SAFE_SIM,
            freshness=SAFE_FRESH,
        )
    with pytest.raises(ValueError):
        classify_tier(
            comparable_count=10,
            effective_competitor_n=-0.5,
            similarity_confidence=SAFE_SIM,
            freshness=SAFE_FRESH,
        )


# --------------------------------------------------------------------------
# classify_tier — the ladder, exactly at and one below every threshold
# --------------------------------------------------------------------------

def _tier_a_kwargs(**overrides):
    kwargs = dict(
        comparable_count=TIER_A_MIN_COMPARABLES,
        effective_competitor_n=TIER_A_MIN_EFFECTIVE_N,
        similarity_confidence=TIER_A_MIN_SIMILARITY,
        freshness=TIER_A_MIN_FRESHNESS,
    )
    kwargs.update(overrides)
    return kwargs


def test_tier_a_exactly_at_every_threshold():
    tier, reason = classify_tier(**_tier_a_kwargs())
    assert tier is EvidenceTier.A
    assert reason is None
    assert tier.allows_competitor_prediction
    assert not tier.requires_widened_interval


def test_tier_a_one_below_effective_n_falls_to_b():
    tier, reason = classify_tier(**_tier_a_kwargs(effective_competitor_n=TIER_A_MIN_EFFECTIVE_N - 0.001))
    assert tier is EvidenceTier.B
    assert reason is None


def test_tier_a_one_below_comparables_falls_to_b():
    tier, _ = classify_tier(**_tier_a_kwargs(comparable_count=TIER_A_MIN_COMPARABLES - 1))
    assert tier is EvidenceTier.B


def test_tier_a_one_below_similarity_falls_to_b():
    tier, _ = classify_tier(**_tier_a_kwargs(similarity_confidence=TIER_A_MIN_SIMILARITY - 1))
    assert tier is EvidenceTier.B


def test_tier_a_one_below_freshness_falls_to_b():
    tier, _ = classify_tier(**_tier_a_kwargs(freshness=TIER_A_MIN_FRESHNESS - 1))
    assert tier is EvidenceTier.B


def test_tier_b_exactly_at_thresholds():
    tier, reason = classify_tier(
        comparable_count=TIER_B_MIN_COMPARABLES,
        effective_competitor_n=TIER_B_MIN_EFFECTIVE_N,
        similarity_confidence=SAFE_SIM,
        freshness=SAFE_FRESH,
    )
    assert tier is EvidenceTier.B
    assert reason is None
    assert tier.allows_competitor_prediction
    assert tier.requires_widened_interval


def test_tier_b_one_below_effective_n_falls_to_c():
    tier, reason = classify_tier(
        comparable_count=TIER_B_MIN_COMPARABLES,
        effective_competitor_n=TIER_B_MIN_EFFECTIVE_N - 0.001,
        similarity_confidence=SAFE_SIM,
        freshness=SAFE_FRESH,
    )
    assert tier is EvidenceTier.C
    assert reason is SuppressionReason.INSUFFICIENT_EVIDENCE


def test_tier_b_one_below_comparables_falls_to_c():
    tier, reason = classify_tier(
        comparable_count=TIER_B_MIN_COMPARABLES - 1,
        effective_competitor_n=TIER_B_MIN_EFFECTIVE_N,
        similarity_confidence=SAFE_SIM,
        freshness=SAFE_FRESH,
    )
    assert tier is EvidenceTier.C
    assert reason is SuppressionReason.INSUFFICIENT_EVIDENCE


def test_tier_c_exactly_at_threshold():
    tier, reason = classify_tier(
        comparable_count=TIER_C_MIN_COMPARABLES,
        effective_competitor_n=0.0,
        similarity_confidence=SAFE_SIM,
        freshness=SAFE_FRESH,
    )
    assert tier is EvidenceTier.C
    assert reason is SuppressionReason.INSUFFICIENT_EVIDENCE
    assert tier.allows_market_prediction
    assert not tier.allows_competitor_prediction


def test_tier_c_one_below_threshold_is_tier_d():
    tier, reason = classify_tier(
        comparable_count=TIER_C_MIN_COMPARABLES - 1,
        effective_competitor_n=0.0,
        similarity_confidence=SAFE_SIM,
        freshness=SAFE_FRESH,
    )
    assert tier is EvidenceTier.D
    assert reason is SuppressionReason.INSUFFICIENT_EVIDENCE
    assert not tier.allows_market_prediction


def test_unbounded_competitor_history_cannot_lift_a_thin_market():
    """The corpus contains vendors with 25 offers. That must not license a
    prediction when there are no comparable tenders to price against."""
    tier, reason = classify_tier(
        comparable_count=TIER_C_MIN_COMPARABLES - 1,
        effective_competitor_n=999.0,
        similarity_confidence=100,
        freshness=100,
    )
    assert tier is EvidenceTier.D
    assert reason is not None


@pytest.mark.parametrize(
    "comparables,effective_n",
    [
        (0, 0.0), (1, 0.0), (4, 3.0), (5, 0.0), (7, 3.9),
        (8, 3.99), (11, 3.5), (30, 0.0), (200, 1.0),
    ],
)
def test_tier_c_and_d_always_carry_a_competitor_suppression_reason(comparables, effective_n):
    tier, reason = classify_tier(
        comparable_count=comparables,
        effective_competitor_n=effective_n,
        similarity_confidence=SAFE_SIM,
        freshness=SAFE_FRESH,
    )
    assert tier in (EvidenceTier.C, EvidenceTier.D)
    assert reason is not None
    assert not tier.allows_competitor_prediction


def test_every_tier_c_or_d_outcome_over_a_wide_sweep_is_suppressed():
    """Exhaustive sweep: no combination may yield C/D with reason None, and no
    combination may yield A/B with a reason set."""
    for comparables in range(30):
        for effective_n in (0.0, 0.5, 3.99, 4.0, 7.99, 8.0, 20.0):
            for sim in (0, 24, 25, 59, 60, 100):
                for fresh in (0, 14, 15, 49, 50, 100):
                    tier, reason = classify_tier(
                        comparable_count=comparables,
                        effective_competitor_n=effective_n,
                        similarity_confidence=sim,
                        freshness=fresh,
                    )
                    if tier.allows_competitor_prediction:
                        assert reason is None
                    else:
                        assert reason is not None


# --------------------------------------------------------------------------
# confidence_score
# --------------------------------------------------------------------------

def _conf(**overrides) -> int:
    kwargs = dict(
        tier=EvidenceTier.B,
        evidence_count=5,
        freshness=50,
        similarity_confidence=50,
        interval_width_ratio=0.4,
    )
    kwargs.update(overrides)
    return confidence_score(**kwargs)


def test_confidence_is_bounded_0_100():
    assert 0 <= _conf(tier=EvidenceTier.D, evidence_count=0, freshness=0,
                      similarity_confidence=0, interval_width_ratio=10.0) <= 100
    assert 0 <= _conf(tier=EvidenceTier.A, evidence_count=10_000, freshness=100,
                      similarity_confidence=100, interval_width_ratio=0.0) <= 100


def test_confidence_monotone_non_decreasing_in_evidence_count():
    previous = -1
    for n in range(200):
        value = _conf(evidence_count=n)
        assert value >= previous, f"dropped at evidence_count={n}"
        previous = value


def test_confidence_monotone_non_decreasing_in_freshness():
    previous = -1
    for fresh in range(101):
        value = _conf(freshness=fresh)
        assert value >= previous, f"dropped at freshness={fresh}"
        previous = value


def test_confidence_monotone_non_decreasing_in_similarity():
    previous = -1
    for sim in range(101):
        value = _conf(similarity_confidence=sim)
        assert value >= previous, f"dropped at similarity={sim}"
        previous = value


def test_confidence_monotone_non_increasing_in_interval_width():
    previous = 101
    for step in range(41):
        ratio = step / 10.0
        value = _conf(interval_width_ratio=ratio)
        assert value <= previous, f"rose at interval_width_ratio={ratio}"
        previous = value


def test_confidence_monotone_in_tier_strength():
    """D <= C <= B <= A holds for every corner of the other inputs."""
    for n in (0, 3, 40):
        for fresh in (0, 55, 100):
            for sim in (0, 55, 100):
                for width in (0.0, 0.5, 3.0):
                    scores = [
                        confidence_score(
                            tier=tier,
                            evidence_count=n,
                            freshness=fresh,
                            similarity_confidence=sim,
                            interval_width_ratio=width,
                        )
                        for tier in (
                            EvidenceTier.D, EvidenceTier.C, EvidenceTier.B, EvidenceTier.A
                        )
                    ]
                    assert scores == sorted(scores), scores


def test_width_penalty_saturates_at_the_cap():
    at_cap = _conf(interval_width_ratio=CONFIDENCE_WIDTH_RATIO_CAP)
    way_past = _conf(interval_width_ratio=CONFIDENCE_WIDTH_RATIO_CAP * 25)
    assert at_cap == way_past


def test_tier_d_confidence_stays_low_however_good_the_rest_looks():
    """Tier D means the system is showing facts only; a high confidence badge
    next to a suppressed number would be a lie about the evidence."""
    assert _conf(tier=EvidenceTier.D, evidence_count=1000, freshness=100,
                 similarity_confidence=100, interval_width_ratio=0.0) <= 25


def test_confidence_accepts_a_tier_string():
    assert confidence_score(
        tier="A", evidence_count=10, freshness=90,
        similarity_confidence=90, interval_width_ratio=0.2,
    ) == confidence_score(
        tier=EvidenceTier.A, evidence_count=10, freshness=90,
        similarity_confidence=90, interval_width_ratio=0.2,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        dict(evidence_count=-1),
        dict(interval_width_ratio=-0.1),
        dict(freshness=101),
        dict(similarity_confidence=-1),
    ],
)
def test_confidence_rejects_invalid_inputs(overrides):
    with pytest.raises(ValueError):
        _conf(**overrides)


def test_confidence_is_not_a_win_probability():
    """Guard on the docstring itself: the disclaimer is part of the contract."""
    doc = confidence_score.__doc__ or ""
    assert "not a win probability" in doc.lower()


# --------------------------------------------------------------------------
# market_evidence
# --------------------------------------------------------------------------

async def test_market_evidence_counts_and_grades_comparables():
    rows = [award_row(30, same_agency=True) for _ in range(6)] + [
        award_row(90) for _ in range(6)
    ]
    conn = FakeConn(rows)
    result = await market_evidence(
        conn, tender={"id": 1, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    assert isinstance(result, MarketEvidence)
    assert result.comparable_count == 12
    assert result.median_age_days == pytest.approx(60.0)
    assert result.freshness > 80
    assert result.similarity_confidence == 78  # 55 + 45 * 0.5, rounded
    # No vendor history was supplied, so a competitor claim is impossible.
    assert result.tier is EvidenceTier.C
    assert result.suppression is None  # market statements are still allowed
    assert not result.tier.allows_competitor_prediction


async def test_market_evidence_passes_the_cutoff_to_the_query():
    conn = FakeConn([])
    await market_evidence(
        conn, tender={"id": 42, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    query, args = conn.calls[0]
    assert "COALESCE(a.awarded_at, a.created_at) < $2" in query
    assert args == (42, AS_OF, 111, 7)


async def test_market_evidence_uses_the_tenders_own_opening_date_by_default():
    opening = datetime(2026, 3, 1, tzinfo=UTC)
    conn = FakeConn([])
    await market_evidence(conn, tender={"id": 1, "activity_id": 111, "offers_opening_date": opening})
    _, args = conn.calls[0]
    assert args[1] == opening


async def test_market_evidence_without_activity_falls_back_to_agency():
    conn = FakeConn([award_row(10) for _ in range(9)])
    result = await market_evidence(
        conn, tender={"id": 1, "activity_id": None, "agency_id": 7}, as_of=AS_OF
    )
    query, args = conn.calls[0]
    assert "t.agency_id = $4" in query
    assert args == (1, AS_OF, None, 7)
    # Likeness of work is unknown, so similarity is capped below the Tier A bar.
    assert result.similarity_confidence == SIM_AGENCY_ONLY
    assert result.similarity_confidence < TIER_A_MIN_SIMILARITY


async def test_market_evidence_with_neither_activity_nor_agency_does_not_query():
    conn = FakeConn([award_row(1)])
    result = await market_evidence(conn, tender={"id": 1}, as_of=AS_OF)
    assert conn.calls == []
    assert result.comparable_count == 0
    assert result.tier is EvidenceTier.D
    assert result.suppression is SuppressionReason.NO_COMPARABLE_TENDERS


async def test_market_evidence_empty_result_is_suppressed_not_zero_priced():
    conn = FakeConn([])
    result = await market_evidence(
        conn, tender={"id": 1, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    assert result.comparable_count == 0
    assert result.similarity_confidence == 0
    assert result.freshness == 0
    assert result.suppression is SuppressionReason.NO_COMPARABLE_TENDERS


async def test_market_evidence_ancient_comparables_are_stale_not_usable():
    conn = FakeConn([award_row(1500, same_agency=True) for _ in range(40)])
    result = await market_evidence(
        conn, tender={"id": 1, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    assert result.freshness == 0
    assert result.tier is EvidenceTier.D
    assert result.suppression is SuppressionReason.STALE_DATA


async def test_market_evidence_to_dict_is_json_safe():
    conn = FakeConn([award_row(10, same_agency=True) for _ in range(6)])
    result = await market_evidence(
        conn, tender={"id": 1, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    payload = result.to_dict()
    assert payload["tier"] == "C"
    assert payload["suppression"] is None
    assert isinstance(payload["comparable_count"], int)


# --------------------------------------------------------------------------
# competitor_evidence
# --------------------------------------------------------------------------

async def test_competitor_evidence_typical_sparse_vendor_is_suppressed():
    """The modal case in this corpus: 2 observed offers. Suppression is the
    correct output; a quantile from two points would be fabrication."""
    market_rows = [award_row(60, same_agency=True) for _ in range(20)]
    vendor_rows = [award_row(60, same_agency=True), award_row(200, same_agency=False)]
    conn = FakeConn(market_rows, vendor_rows)
    result = await competitor_evidence(
        conn, vendor_id=99, tender={"id": 1, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    assert isinstance(result, CompetitorEvidence)
    assert result.observation_count == 2
    assert result.effective_n < TIER_B_MIN_EFFECTIVE_N
    assert result.tier is EvidenceTier.C
    assert result.suppression is SuppressionReason.INSUFFICIENT_EVIDENCE
    assert not result.tier.allows_competitor_prediction


async def test_competitor_evidence_rich_vendor_reaches_tier_a():
    market_rows = [award_row(30, same_agency=True) for _ in range(40)]
    vendor_rows = [award_row(30, same_agency=True, same_activity=True) for _ in range(10)]
    conn = FakeConn(market_rows, vendor_rows)
    result = await competitor_evidence(
        conn, vendor_id=99, tender={"id": 1, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    assert result.observation_count == 10
    assert result.agency_overlap_count == 10
    assert result.activity_overlap_count == 10
    assert result.effective_n >= TIER_A_MIN_EFFECTIVE_N
    assert result.tier is EvidenceTier.A
    assert result.suppression is None


async def test_competitor_evidence_recency_decay_can_demote_a_tier():
    """Ten observations, all four years old, are not ten current observations."""
    market_rows = [award_row(30, same_agency=True) for _ in range(40)]
    vendor_rows = [award_row(4 * 365, same_agency=True) for _ in range(10)]
    conn = FakeConn(market_rows, vendor_rows)
    result = await competitor_evidence(
        conn, vendor_id=99, tender={"id": 1, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    assert result.observation_count == 10
    assert result.effective_n == pytest.approx(10 * 0.5 ** 4, abs=1e-3)
    assert result.freshness < HARD_STALE_FLOOR_FRESHNESS
    assert result.tier is EvidenceTier.D
    assert result.suppression is SuppressionReason.STALE_DATA


async def test_competitor_evidence_no_history_is_suppressed_and_keeps_market_freshness():
    market_rows = [award_row(30, same_agency=True) for _ in range(20)]
    conn = FakeConn(market_rows, [])
    result = await competitor_evidence(
        conn, vendor_id=99, tender={"id": 1, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    assert result.observation_count == 0
    assert result.effective_n == 0.0
    assert result.median_age_days == pytest.approx(30.0)  # fell back to the market
    assert result.suppression is SuppressionReason.INSUFFICIENT_EVIDENCE


async def test_competitor_evidence_query_is_point_in_time_and_award_gated():
    conn = FakeConn([], [])
    await competitor_evidence(
        conn, vendor_id=99, tender={"id": 42, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    query, args = conn.calls[1]
    assert "JOIN awards a ON a.tender_id = t.id" in query
    assert "COALESCE(a.awarded_at, a.created_at) < $2" in query
    assert "t.id <> $1" in query  # never learn from the tender being predicted
    assert args == (42, AS_OF, 111, 7, 99)


async def test_competitor_evidence_to_dict_is_json_safe():
    conn = FakeConn([award_row(30, same_agency=True) for _ in range(20)], [award_row(30)])
    result = await competitor_evidence(
        conn, vendor_id=99, tender={"id": 1, "activity_id": 111, "agency_id": 7}, as_of=AS_OF
    )
    payload = result.to_dict()
    assert payload["tier"] in {"A", "B", "C", "D"}
    assert payload["suppression"] == "INSUFFICIENT_EVIDENCE"
    assert payload["observation_count"] == 1


# --------------------------------------------------------------------------
# Optional: live database sanity check (read-only, skipped when unavailable)
# --------------------------------------------------------------------------

LIVE_DSN = "postgres://thaqip:thaqip_dev@localhost:5433/thaqip"


async def test_against_live_corpus_is_structurally_sound():
    """Read-only smoke test. Asserts invariants, never specific numbers, so it
    stays deterministic as the corpus grows."""
    asyncpg = pytest.importorskip("asyncpg")
    try:
        conn = await asyncpg.connect(LIVE_DSN, timeout=3)
    except Exception as exc:  # noqa: BLE001 - any connect failure means "skip"  # pragma: no cover
        pytest.skip(f"live database unavailable: {exc}")
    try:
        row = await conn.fetchrow(
            "SELECT t.id, t.activity_id, t.agency_id, t.offers_opening_date, t.last_offer_date "
            "FROM tenders t JOIN awards a ON a.tender_id = t.id "
            "WHERE t.activity_id IS NOT NULL ORDER BY t.id LIMIT 1"
        )
        if row is None:  # pragma: no cover - empty corpus
            pytest.skip("no awarded tenders in the live corpus")
        tender = dict(row)
        market = await market_evidence(conn, tender=tender)
        assert market.comparable_count >= 0
        assert 0 <= market.freshness <= 100
        assert 0 <= market.similarity_confidence <= 100
        assert (market.suppression is None) == market.tier.allows_market_prediction

        vendor = await conn.fetchrow(
            "SELECT vendor_id FROM offers WHERE vendor_id IS NOT NULL "
            "GROUP BY vendor_id ORDER BY count(*) DESC, vendor_id LIMIT 1"
        )
        if vendor is not None:
            comp = await competitor_evidence(conn, vendor_id=vendor["vendor_id"], tender=tender)
            assert comp.effective_n <= comp.observation_count + 1e-9
            assert (comp.suppression is None) == comp.tier.allows_competitor_prediction
    finally:
        await conn.close()
