"""Tests for p2w.competitor — deterministic, no network, no database writes.

The two things that can genuinely hurt a user here are (a) a confident-looking
competitor quantile built on two bids and (b) an undercut risk that moves the
wrong way. Both get direct tests. The shrinkage table at n = 1, 2, 5, 30 is the
centrepiece: it is the mechanism that stops (a) from happening.
"""
from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from thaqip_ingestion.p2w import competitor as comp
from thaqip_ingestion.p2w.contracts import (
    MODEL_VERSION,
    EvidenceTier,
    PredictionScope,
    PricePrediction,
    Quantiles,
    SuppressionReason,
)

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)

TENDER: dict[str, Any] = {
    "id": 4242,
    "activity_id": 111,
    "agency_id": 7,
    "offers_opening_date": AS_OF,
}


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class RowConn:
    """Asyncpg-shaped stand-in that replays canned rows per SQL statement."""

    def __init__(self, vendor_rows: list[dict[str, Any]], prior_rows: list[dict[str, Any]]):
        self.vendor_rows = vendor_rows
        self.prior_rows = prior_rows
        self.calls: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(args)
        if "o.vendor_id = $1" in sql:
            return list(self.vendor_rows)
        return list(self.prior_rows)


def vendor_row(log_ratio: float, *, age_days: float = 0.0, same_activity: bool = True):
    return {
        "log_ratio": log_ratio,
        "knowable_at": AS_OF - timedelta(days=age_days),
        "same_activity": same_activity,
    }


def prior_row(log_ratio: float, *, age_days: float = 0.0):
    return {"log_ratio": log_ratio, "knowable_at": AS_OF - timedelta(days=age_days)}


class FakeEvidence:
    """Stands in for evidence.CompetitorEvidence."""

    def __init__(
        self,
        tier: EvidenceTier,
        *,
        observation_count: int = 10,
        effective_n: float = 10.0,
        suppression: SuppressionReason | None = None,
    ):
        self.tier = tier
        self.comparable_count = 20
        self.median_age_days = 30.0
        self.freshness = 95
        self.similarity_confidence = 80
        self.suppression = suppression or (
            None if tier.allows_competitor_prediction else SuppressionReason.INSUFFICIENT_EVIDENCE
        )
        self.observation_count = observation_count
        self.agency_overlap_count = 2
        self.activity_overlap_count = observation_count
        self.effective_n = effective_n


def patch_evidence(monkeypatch, ev: FakeEvidence) -> None:
    from thaqip_ingestion.p2w import evidence as evidence_mod

    async def _competitor_evidence(conn, **kwargs):
        return ev

    monkeypatch.setattr(evidence_mod, "competitor_evidence", _competitor_evidence)


def patch_market(monkeypatch, prediction: PricePrediction) -> None:
    from thaqip_ingestion.p2w import market as market_mod

    async def _market_quantiles(conn, **kwargs):
        return prediction

    monkeypatch.setattr(market_mod, "market_quantiles", _market_quantiles)


def market_ok(p50: float = 1_000_000.0) -> PricePrediction:
    return PricePrediction(
        tender_id=TENDER["id"],
        prediction_scope=PredictionScope.MARKET,
        p10=p50 * 0.8,
        p50=p50,
        p90=p50 * 1.3,
        confidence_score=70,
        evidence_count=20,
        evidence_tier=EvidenceTier.A,
    )


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def test_recency_weight_halves_at_the_half_life():
    assert comp.recency_weight(0.0) == 1.0
    assert comp.recency_weight(comp.RECENCY_HALF_LIFE_DAYS) == pytest.approx(0.5)
    assert comp.recency_weight(2 * comp.RECENCY_HALF_LIFE_DAYS) == pytest.approx(0.25)


def test_recency_weight_clamps_future_records_to_fresh():
    assert comp.recency_weight(-500.0) == 1.0


def test_recency_weight_rejects_nonpositive_half_life():
    with pytest.raises(ValueError):
        comp.recency_weight(10.0, half_life_days=0.0)


def test_effective_n_equals_count_for_equal_weights():
    assert comp.effective_n([1.0] * 7) == pytest.approx(7.0)
    assert comp.effective_n([0.3] * 7) == pytest.approx(7.0)


def test_effective_n_shrinks_when_one_observation_dominates():
    assert comp.effective_n([1.0, 0.01, 0.01]) < 1.2
    assert comp.effective_n([]) == 0.0


def test_weighted_mean_matches_hand_computation():
    assert comp.weighted_mean([1.0, 3.0], [1.0, 3.0]) == pytest.approx(2.5)
    assert comp.weighted_mean([], []) == 0.0


def test_weighted_mean_rejects_length_mismatch():
    with pytest.raises(ValueError):
        comp.weighted_mean([1.0], [1.0, 2.0])


def test_weighted_sd_reduces_to_sample_sd_for_unit_weights():
    values = [1.0, 2.0, 3.0, 4.0]
    expected = math.sqrt(sum((v - 2.5) ** 2 for v in values) / 3.0)
    assert comp.weighted_sd(values, [1.0] * 4) == pytest.approx(expected)


def test_weighted_sd_of_a_single_observation_is_zero_not_a_guess():
    assert comp.weighted_sd([1.7], [1.0]) == 0.0


def test_fit_log_ratios_on_empty_sample_reports_zero_evidence():
    mu, sigma, n = comp.fit_log_ratios([])
    assert (mu, sigma, n) == (0.0, 0.0, 0.0)


# --------------------------------------------------------------------------
# shrinkage — the core safety mechanism
# --------------------------------------------------------------------------

PRIOR_MU, PRIOR_SIGMA = 0.40, 0.65  # close to the real corpus-wide fit
VENDOR_MU, VENDOR_SIGMA = 0.05, 0.30  # a disciplined vendor, like vendor 198


@pytest.mark.parametrize("n,expected_w", [(1, 1 / 6), (2, 2 / 7), (5, 0.5), (30, 30 / 35)])
def test_shrink_pooling_weight_follows_the_documented_formula(n, expected_w):
    mu_post, _ = comp.shrink(VENDOR_MU, n, PRIOR_MU, PRIOR_SIGMA, VENDOR_SIGMA)
    assert mu_post == pytest.approx(expected_w * VENDOR_MU + (1 - expected_w) * PRIOR_MU)


def test_shrink_mu_moves_monotonically_from_prior_to_vendor_mean():
    mus = [comp.shrink(VENDOR_MU, n, PRIOR_MU, PRIOR_SIGMA, VENDOR_SIGMA)[0] for n in (1, 2, 5, 30)]
    # vendor_mu < prior_mu here, so the sequence must be strictly decreasing.
    assert mus == sorted(mus, reverse=True)
    assert all(a > b for a, b in itertools.pairwise(mus))


def test_shrink_with_two_observations_stays_close_to_the_prior():
    mu_post, _ = comp.shrink(VENDOR_MU, 2, PRIOR_MU, PRIOR_SIGMA, VENDOR_SIGMA)
    distance_to_prior = abs(mu_post - PRIOR_MU)
    distance_to_vendor = abs(mu_post - VENDOR_MU)
    assert distance_to_prior < distance_to_vendor
    # Quantitatively: no more than a third of the way toward the vendor.
    assert distance_to_prior <= 0.34 * abs(VENDOR_MU - PRIOR_MU)


def test_shrink_with_thirty_observations_is_close_to_the_vendor_mean():
    mu_post, _ = comp.shrink(VENDOR_MU, 30, PRIOR_MU, PRIOR_SIGMA, VENDOR_SIGMA)
    assert abs(mu_post - VENDOR_MU) < 0.15 * abs(VENDOR_MU - PRIOR_MU) + 0.06
    assert abs(mu_post - VENDOR_MU) < abs(mu_post - PRIOR_MU)


def test_shrink_at_zero_observations_is_exactly_the_prior():
    mu_post, sigma_post = comp.shrink(VENDOR_MU, 0, PRIOR_MU, PRIOR_SIGMA, VENDOR_SIGMA)
    assert mu_post == pytest.approx(PRIOR_MU)
    assert sigma_post == pytest.approx(PRIOR_SIGMA)


@pytest.mark.parametrize("n", [1, 2, 5, 10, 14])
def test_shrink_sigma_never_narrower_than_the_prior_at_low_n(n):
    """A thin vendor may not look more predictable than its category."""
    _, sigma_post = comp.shrink(VENDOR_MU, n, PRIOR_MU, PRIOR_SIGMA, VENDOR_SIGMA)
    assert sigma_post >= PRIOR_SIGMA


def test_shrink_sigma_may_go_below_the_prior_once_the_vendor_is_well_observed():
    _, sigma_post = comp.shrink(VENDOR_MU, 60, PRIOR_MU, PRIOR_SIGMA, VENDOR_SIGMA)
    assert sigma_post < PRIOR_SIGMA


def test_shrink_widens_when_a_sparse_vendor_disagrees_with_its_category():
    """The mixture disagreement term must punish disagreement, not average it away."""
    _, agreeing = comp.shrink(PRIOR_MU, 3, PRIOR_MU, PRIOR_SIGMA, VENDOR_SIGMA)
    _, disagreeing = comp.shrink(PRIOR_MU + 2.0, 3, PRIOR_MU, PRIOR_SIGMA, VENDOR_SIGMA)
    assert disagreeing > agreeing


def test_shrink_honours_the_absolute_sigma_floor():
    _, sigma_post = comp.shrink(0.0, 1000, 0.0, 0.0, 0.0)
    assert sigma_post == pytest.approx(comp.MIN_SIGMA)


def test_shrink_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        comp.shrink(0.0, -1, 0.0, 0.1, 0.1)
    with pytest.raises(ValueError):
        comp.shrink(0.0, 1, 0.0, -0.1, 0.1)


# --------------------------------------------------------------------------
# log -> SAR transform
# --------------------------------------------------------------------------


def test_log_quantiles_are_ordered_and_centred_on_mu():
    low, mid, high = comp.log_quantiles(0.3, 0.5)
    assert low < mid < high
    assert mid == pytest.approx(0.3)
    assert (high - mid) == pytest.approx(mid - low)


def test_log_quantiles_widening_keeps_the_median_and_widens_the_band():
    narrow = comp.log_quantiles(0.3, 0.5)
    wide = comp.log_quantiles(0.3, 0.5, widen=comp.TIER_B_WIDENING_FACTOR)
    assert wide[1] == pytest.approx(narrow[1])
    assert (wide[2] - wide[0]) == pytest.approx(
        comp.TIER_B_WIDENING_FACTOR * (narrow[2] - narrow[0])
    )


def test_widening_factor_meets_the_specified_floor():
    assert comp.TIER_B_WIDENING_FACTOR >= 1.3


@pytest.mark.parametrize("mu", [-1.5, 0.0, 0.4, 2.0])
@pytest.mark.parametrize("sigma", [0.02, 0.3, 1.2])
@pytest.mark.parametrize("baseline", [1.0, 12_345.0, 9_800_000.0])
def test_quantile_ordering_survives_the_log_ratio_to_sar_transform(mu, sigma, baseline):
    q = comp.to_sar(comp.log_quantiles(mu, sigma), baseline)
    assert q.p10 <= q.p50 <= q.p90
    assert q.p10 > 0
    q.validate()  # raises if the contract invariant is broken


def test_to_sar_recovers_the_baseline_when_the_ratio_is_one():
    q = comp.to_sar((0.0, 0.0, 0.0), 500_000.0)
    assert q.p50 == pytest.approx(500_000.0)


def test_to_sar_rejects_a_nonpositive_baseline():
    with pytest.raises(ValueError):
        comp.to_sar((0.0, 0.0, 0.0), 0.0)


def test_implied_sigma_round_trips_the_fitted_shape():
    q = comp.to_sar(comp.log_quantiles(0.4, 0.55), 1_000_000.0)
    prediction = PricePrediction(
        tender_id=1,
        prediction_scope=PredictionScope.COMPETITOR,
        p10=q.p10,
        p50=q.p50,
        p90=q.p90,
    )
    assert comp.implied_sigma(prediction) == pytest.approx(0.55)


def test_implied_sigma_is_none_for_a_suppressed_prediction():
    suppressed = PricePrediction.suppressed(
        tender_id=1,
        scope=PredictionScope.COMPETITOR,
        reason=SuppressionReason.INSUFFICIENT_EVIDENCE,
    )
    assert comp.implied_sigma(suppressed) is None


# --------------------------------------------------------------------------
# undercut risk (FR-006)
# --------------------------------------------------------------------------


def fitted(p50: float, sigma: float = 0.4) -> PricePrediction:
    q = comp.to_sar(comp.log_quantiles(0.0, sigma), p50)
    return PricePrediction(
        tender_id=1,
        prediction_scope=PredictionScope.COMPETITOR,
        subject_id=9,
        p10=q.p10,
        p50=q.p50,
        p90=q.p90,
    )


@pytest.mark.parametrize("bid", [1.0, 50_000.0, 1_000_000.0, 5_000_000.0, 1e9])
def test_undercut_risk_is_a_probability(bid):
    risk = comp.undercut_risk(bid, fitted(1_000_000.0))
    assert risk is not None
    assert 0.0 <= risk <= 1.0


def test_undercut_risk_at_the_competitor_median_is_one_half():
    assert comp.undercut_risk(1_000_000.0, fitted(1_000_000.0)) == pytest.approx(0.5)


def test_undercut_risk_at_the_competitor_p10_is_ten_percent():
    competitor = fitted(1_000_000.0)
    assert comp.undercut_risk(competitor.p10, competitor) == pytest.approx(0.10, abs=1e-6)


def test_undercut_risk_is_monotone_decreasing_in_competitor_p50():
    bid = 1_000_000.0
    risks = [comp.undercut_risk(bid, fitted(p50)) for p50 in (400_000, 700_000, 1_000_000, 2_000_000, 5_000_000)]
    assert all(a > b for a, b in itertools.pairwise(risks))


def test_undercut_risk_is_monotone_increasing_in_the_users_own_bid():
    competitor = fitted(1_000_000.0)
    risks = [comp.undercut_risk(b, competitor) for b in (200_000, 600_000, 1_000_000, 3_000_000)]
    assert all(a < b for a, b in itertools.pairwise(risks))


def test_undercut_risk_is_none_when_the_competitor_is_suppressed():
    suppressed = PricePrediction.suppressed(
        tender_id=1,
        scope=PredictionScope.COMPETITOR,
        subject_id=9,
        reason=SuppressionReason.INSUFFICIENT_EVIDENCE,
    )
    assert comp.undercut_risk(1_000_000.0, suppressed) is None


def test_undercut_risk_of_a_nonpositive_bid_is_zero():
    assert comp.undercut_risk(0.0, fitted(1_000_000.0)) == 0.0
    assert comp.undercut_risk(-5.0, fitted(1_000_000.0)) == 0.0


def test_undercut_risk_widens_toward_one_half_for_a_wider_band():
    """A vaguer competitor estimate must produce a less decisive risk."""
    bid = 1_400_000.0
    tight = comp.undercut_risk(bid, fitted(1_000_000.0, sigma=0.15))
    loose = comp.undercut_risk(bid, fitted(1_000_000.0, sigma=1.2))
    assert tight > loose > 0.5


# --------------------------------------------------------------------------
# sample builders
# --------------------------------------------------------------------------


async def test_competitor_ratio_sample_weights_by_recency():
    conn = RowConn([vendor_row(0.2), vendor_row(0.4, age_days=365.0)], [])
    sample = await comp.competitor_ratio_sample(
        conn, vendor_id=5, activity_id=111, as_of=AS_OF
    )
    assert [v for v, _ in sample] == [0.2, 0.4]
    assert sample[0][1] == pytest.approx(1.0)
    assert sample[1][1] == pytest.approx(0.5)


async def test_competitor_ratio_sample_downweights_other_activities():
    conn = RowConn([vendor_row(0.2, same_activity=False)], [])
    sample = await comp.competitor_ratio_sample(
        conn, vendor_id=5, activity_id=111, as_of=AS_OF
    )
    assert sample[0][1] == pytest.approx(comp.CROSS_ACTIVITY_WEIGHT)


async def test_competitor_ratio_sample_does_not_downweight_when_no_activity_known():
    conn = RowConn([vendor_row(0.2, same_activity=False)], [])
    sample = await comp.competitor_ratio_sample(
        conn, vendor_id=5, activity_id=None, as_of=AS_OF
    )
    assert sample[0][1] == pytest.approx(1.0)


async def test_competitor_ratio_sample_passes_the_point_in_time_cutoff_to_sql():
    conn = RowConn([], [])
    await comp.competitor_ratio_sample(conn, vendor_id=5, activity_id=111, as_of=AS_OF)
    # $4 is the subject tender to exclude; None when the caller names none.
    assert conn.calls[0] == (5, AS_OF, 111, None)


async def test_competitor_ratio_sample_passes_the_subject_tender_to_exclude():
    conn = RowConn([], [])
    await comp.competitor_ratio_sample(
        conn, vendor_id=5, activity_id=111, as_of=AS_OF, tender_id=42
    )
    assert conn.calls[0] == (5, AS_OF, 111, 42)


async def test_competitor_ratio_sample_skips_unusable_rows():
    conn = RowConn([vendor_row(0.2), {"log_ratio": None, "knowable_at": AS_OF,
                                      "same_activity": True}], [])
    sample = await comp.competitor_ratio_sample(
        conn, vendor_id=5, activity_id=111, as_of=AS_OF
    )
    assert len(sample) == 1


async def test_category_prior_fits_mu_and_sigma():
    rows = [prior_row(v) for v in (0.0, 0.5, 1.0)]
    conn = RowConn([], rows)
    mu, sigma = await comp.category_prior(conn, activity_id=None, as_of=AS_OF)
    assert mu == pytest.approx(0.5)
    assert sigma == pytest.approx(0.5)


async def test_category_prior_falls_back_to_the_whole_corpus_when_thin():
    """A category with fewer than MIN_PRIOR_OBSERVATIONS rows re-queries globally."""
    calls: list[Any] = []

    class ThinConn:
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            calls.append(args[0])
            if args[0] is None:
                return [prior_row(0.4) for _ in range(20)]
            return [prior_row(0.9), prior_row(1.1)]

    sample = await comp.category_prior_sample(ThinConn(), activity_id=111, as_of=AS_OF)
    assert calls == [111, None]
    assert len(sample) == 20


async def test_category_prior_keeps_a_rich_category():
    rows = [prior_row(0.3) for _ in range(comp.MIN_PRIOR_OBSERVATIONS)]
    conn = RowConn([], rows)
    sample = await comp.category_prior_sample(conn, activity_id=111, as_of=AS_OF)
    assert len(sample) == comp.MIN_PRIOR_OBSERVATIONS
    assert len(conn.calls) == 1  # no fallback query


# --------------------------------------------------------------------------
# competitor_quantiles — suppression gates
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tier", [EvidenceTier.C, EvidenceTier.D])
async def test_tier_c_and_d_suppress_with_insufficient_evidence(monkeypatch, tier):
    patch_evidence(monkeypatch, FakeEvidence(tier, observation_count=2, effective_n=2.0))
    patch_market(monkeypatch, market_ok())
    conn = RowConn([vendor_row(0.2)] * 30, [prior_row(0.4)] * 40)

    result = await comp.competitor_quantiles(
        conn, vendor_id=9, tender=TENDER, as_of=AS_OF
    )

    assert result.is_suppressed
    assert result.suppression_reason is SuppressionReason.INSUFFICIENT_EVIDENCE
    assert result.p10 is None and result.p50 is None and result.p90 is None
    assert result.prediction_scope is PredictionScope.COMPETITOR
    assert result.subject_id == 9
    assert result.evidence_tier is tier
    assert result.explanation_factors  # the absence must be explainable


@pytest.mark.parametrize("tier", [EvidenceTier.C, EvidenceTier.D])
async def test_suppressed_tiers_never_touch_the_price_data(monkeypatch, tier):
    """The tier gate is a veto, checked before any ratio query is issued."""
    patch_evidence(monkeypatch, FakeEvidence(tier))
    patch_market(monkeypatch, market_ok())
    conn = RowConn([vendor_row(0.2)] * 30, [prior_row(0.4)] * 40)

    await comp.competitor_quantiles(conn, vendor_id=9, tender=TENDER, as_of=AS_OF)
    assert conn.calls == []


async def test_suppressed_market_baseline_propagates_its_reason(monkeypatch):
    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.A))
    suppressed_market = PricePrediction.suppressed(
        tender_id=TENDER["id"],
        scope=PredictionScope.MARKET,
        reason=SuppressionReason.NO_COMPARABLE_TENDERS,
    )
    patch_market(monkeypatch, suppressed_market)
    conn = RowConn([vendor_row(0.2)] * 30, [prior_row(0.4)] * 40)

    result = await comp.competitor_quantiles(conn, vendor_id=9, tender=TENDER, as_of=AS_OF)
    assert result.is_suppressed
    assert result.suppression_reason is SuppressionReason.NO_COMPARABLE_TENDERS


async def test_empty_vendor_sample_suppresses_even_at_tier_a(monkeypatch):
    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.A))
    conn = RowConn([], [prior_row(0.4)] * 40)

    result = await comp.competitor_quantiles(
        conn, vendor_id=9, tender=TENDER, as_of=AS_OF, market=market_ok()
    )
    assert result.is_suppressed
    assert result.suppression_reason is SuppressionReason.INSUFFICIENT_EVIDENCE


async def test_thin_prior_suppresses_rather_than_inventing_a_category(monkeypatch):
    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.A))
    conn = RowConn([vendor_row(0.2)] * 30, [prior_row(0.4)] * 3)

    result = await comp.competitor_quantiles(
        conn, vendor_id=9, tender=TENDER, as_of=AS_OF, market=market_ok()
    )
    assert result.is_suppressed
    assert result.suppression_reason is SuppressionReason.NO_COMPARABLE_TENDERS


# --------------------------------------------------------------------------
# competitor_quantiles — the happy paths
# --------------------------------------------------------------------------


async def test_tier_a_produces_an_ordered_prediction_with_full_provenance(monkeypatch):
    patch_evidence(
        monkeypatch, FakeEvidence(EvidenceTier.A, observation_count=25, effective_n=24.0)
    )
    conn = RowConn(
        [vendor_row(0.05 + 0.01 * i) for i in range(25)],
        [prior_row(0.4 + 0.02 * i) for i in range(40)],
    )

    result = await comp.competitor_quantiles(
        conn, vendor_id=198, tender=TENDER, as_of=AS_OF, market=market_ok()
    )

    assert not result.is_suppressed
    assert result.p10 < result.p50 < result.p90
    assert result.prediction_scope is PredictionScope.COMPETITOR
    assert result.subject_id == 198
    assert result.evidence_tier is EvidenceTier.A
    assert result.model_version == MODEL_VERSION
    assert result.evidence_count == 25
    assert result.confidence_score is not None and 0 <= result.confidence_score <= 100
    assert result.data_freshness_score == 95
    assert result.feature_snapshot_id
    assert result.win_probability is None  # not this model's question
    Quantiles(p10=result.p10, p50=result.p50, p90=result.p90).validate()


async def test_prediction_carries_distinct_observed_derived_and_predicted_factors(monkeypatch):
    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.A, observation_count=25))
    conn = RowConn([vendor_row(0.05)] * 25, [prior_row(0.4 + 0.02 * i) for i in range(40)])

    result = await comp.competitor_quantiles(
        conn, vendor_id=198, tender=TENDER, as_of=AS_OF, market=market_ok()
    )
    kinds = {f.kind for f in result.explanation_factors}
    assert {"observed", "derived", "predicted"} <= kinds
    baseline_factor = next(
        f for f in result.explanation_factors if f.name == "market_baseline_p50"
    )
    assert baseline_factor.kind == "predicted"  # never dressed up as observed


async def test_tier_b_widens_the_interval_and_docks_confidence(monkeypatch):
    vendor_rows = [vendor_row(0.05 + 0.01 * i) for i in range(25)]
    prior_rows = [prior_row(0.4 + 0.02 * i) for i in range(40)]

    async def run(tier: EvidenceTier) -> PricePrediction:
        patch_evidence(monkeypatch, FakeEvidence(tier, observation_count=25, effective_n=24.0))
        return await comp.competitor_quantiles(
            RowConn(vendor_rows, prior_rows),
            vendor_id=198,
            tender=TENDER,
            as_of=AS_OF,
            market=market_ok(),
        )

    a = await run(EvidenceTier.A)
    b = await run(EvidenceTier.B)

    assert b.p50 == pytest.approx(a.p50)  # widening is symmetric about the median
    assert comp.implied_sigma(b) == pytest.approx(
        comp.TIER_B_WIDENING_FACTOR * comp.implied_sigma(a)
    )
    assert b.confidence_score < a.confidence_score


async def test_a_sparse_tier_a_vendor_is_pulled_toward_its_category(monkeypatch):
    """Two bids must not move the answer far from the category prior."""
    prior_rows = [prior_row(0.4 + 0.02 * i) for i in range(40)]

    async def run(vendor_obs: int) -> PricePrediction:
        patch_evidence(
            monkeypatch,
            FakeEvidence(EvidenceTier.A, observation_count=vendor_obs, effective_n=vendor_obs),
        )
        return await comp.competitor_quantiles(
            RowConn([vendor_row(-1.0)] * vendor_obs, prior_rows),
            vendor_id=198,
            tender=TENDER,
            as_of=AS_OF,
            market=market_ok(),
        )

    sparse = await run(2)
    rich = await run(40)

    baseline_prior_p50 = 1_000_000.0 * math.exp(0.79)  # ~ the prior mean of 0.79
    # The sparse vendor sits far nearer the category than the rich one does.
    assert abs(sparse.p50 - baseline_prior_p50) < abs(rich.p50 - baseline_prior_p50)
    # And it is reported as vaguer, not sharper.
    assert comp.implied_sigma(sparse) > comp.implied_sigma(rich)


async def test_feature_snapshot_id_is_deterministic_and_evidence_sensitive(monkeypatch):
    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.A, observation_count=25))
    prior_rows = [prior_row(0.4 + 0.02 * i) for i in range(40)]

    async def run(rows) -> PricePrediction:
        return await comp.competitor_quantiles(
            RowConn(rows, prior_rows),
            vendor_id=198,
            tender=TENDER,
            as_of=AS_OF,
            market=market_ok(),
        )

    base_rows = [vendor_row(0.05 + 0.01 * i) for i in range(25)]
    first = await run(base_rows)
    second = await run(list(base_rows))
    changed = await run(base_rows + [vendor_row(0.9)])

    assert first.feature_snapshot_id == second.feature_snapshot_id
    assert changed.feature_snapshot_id != first.feature_snapshot_id


async def test_prediction_is_reproducible_run_to_run(monkeypatch):
    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.A, observation_count=25))
    rows = [vendor_row(0.05 + 0.01 * i) for i in range(25)]
    prior_rows = [prior_row(0.4 + 0.02 * i) for i in range(40)]

    results = [
        await comp.competitor_quantiles(
            RowConn(rows, prior_rows),
            vendor_id=198,
            tender=TENDER,
            as_of=AS_OF,
            market=market_ok(),
        )
        for _ in range(3)
    ]
    assert len({(r.p10, r.p50, r.p90) for r in results}) == 1
    assert results[0].seed is None  # closed form: nothing random to seed


async def test_undercut_risk_composes_with_a_real_prediction(monkeypatch):
    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.A, observation_count=25))
    conn = RowConn(
        [vendor_row(0.05 + 0.01 * i) for i in range(25)],
        [prior_row(0.4 + 0.02 * i) for i in range(40)],
    )
    prediction = await comp.competitor_quantiles(
        conn, vendor_id=198, tender=TENDER, as_of=AS_OF, market=market_ok()
    )
    risk_low = comp.undercut_risk(prediction.p10, prediction)
    risk_high = comp.undercut_risk(prediction.p90, prediction)
    assert risk_low == pytest.approx(0.10, abs=1e-6)
    assert risk_high == pytest.approx(0.90, abs=1e-6)


async def test_expected_value_sits_above_the_median_for_a_lognormal(monkeypatch):
    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.A, observation_count=25))
    conn = RowConn(
        [vendor_row(0.05 + 0.01 * i) for i in range(25)],
        [prior_row(0.4 + 0.02 * i) for i in range(40)],
    )
    result = await comp.competitor_quantiles(
        conn, vendor_id=198, tender=TENDER, as_of=AS_OF, market=market_ok()
    )
    assert result.expected_value > result.p50


async def test_prediction_scales_linearly_with_the_market_baseline(monkeypatch):
    """The ratio model is scale-free: double the baseline, double every quantile."""
    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.A, observation_count=25))
    rows = [vendor_row(0.05 + 0.01 * i) for i in range(25)]
    prior_rows = [prior_row(0.4 + 0.02 * i) for i in range(40)]

    async def run(p50: float) -> PricePrediction:
        return await comp.competitor_quantiles(
            RowConn(rows, prior_rows),
            vendor_id=198,
            tender=TENDER,
            as_of=AS_OF,
            market=market_ok(p50),
        )

    one = await run(1_000_000.0)
    two = await run(2_000_000.0)
    assert two.p50 == pytest.approx(2 * one.p50)
    assert two.p10 == pytest.approx(2 * one.p10)
    assert two.p90 == pytest.approx(2 * one.p90)


async def test_to_dict_is_json_safe_for_both_outcomes(monkeypatch):
    import json

    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.A, observation_count=25))
    conn = RowConn(
        [vendor_row(0.05 + 0.01 * i) for i in range(25)],
        [prior_row(0.4 + 0.02 * i) for i in range(40)],
    )
    ok = await comp.competitor_quantiles(
        conn, vendor_id=198, tender=TENDER, as_of=AS_OF, market=market_ok()
    )
    json.dumps(ok.to_dict())

    patch_evidence(monkeypatch, FakeEvidence(EvidenceTier.C))
    bad = await comp.competitor_quantiles(
        conn, vendor_id=198, tender=TENDER, as_of=AS_OF, market=market_ok()
    )
    payload = json.loads(json.dumps(bad.to_dict()))
    assert payload["is_suppressed"] is True
    assert payload["suppression_reason"] == "INSUFFICIENT_EVIDENCE"
