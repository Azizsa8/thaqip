"""Tests for the P2W participation model.

Deterministic throughout: fixed timestamps, no randomness, no sleeps, no
network. The DB-touching functions are exercised against a fake connection
returning canned rows; two optional tests talk to the live database read-only
and skip themselves when it is not reachable. Nothing here writes to the DB.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from thaqip_ingestion.p2w.contracts import MODEL_VERSION, SuppressionReason
from thaqip_ingestion.p2w.participation import (
    BASIS_ACTIVITY,
    BASIS_ACTIVITY_AND_AGENCY,
    BASIS_AGENCY,
    BASIS_INSUFFICIENT,
    BASIS_NO_EVIDENCE,
    DEFAULT_BASE_RATE,
    HALF_LIFE_DAYS,
    MIN_BASE_RATE_TENDERS,
    MIN_CALIBRATION_EVENTS,
    MIN_EVIDENCE_FOR_PREDICTION,
    PARTICIPATION_COEFFICIENTS,
    PROBABILITY_CEILING,
    PROBABILITY_FLOOR,
    ParticipationEstimate,
    ParticipationFeatures,
    base_rate_from_rows,
    brier_score,
    build_features,
    calibrate,
    candidate_bidders,
    estimate_from_features,
    logistic,
    logit,
    normalise_history_rows,
    participation_probability,
    reliability_buckets,
    resolve_as_of,
    summarise_calibration,
)

AS_OF = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ACTIVITY = 111
AGENCY = 55
TENDER = {"id": 9000, "activity_id": ACTIVITY, "agency_id": AGENCY}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class FakeConn:
    """Returns a canned row list per fetch, and records the queries it saw."""

    def __init__(self, *responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.calls.append((query, args))
        if not self._responses:
            return []
        return self._responses.pop(0)


def row(
    vendor_id: int,
    *,
    tender_id: int,
    activity_id: int | None = ACTIVITY,
    agency_id: int | None = AGENCY,
    days_ago: float = 30.0,
    cutoff: datetime | None = None,
) -> dict:
    out = {
        "vendor_id": vendor_id,
        "tender_id": tender_id,
        "activity_id": activity_id,
        "agency_id": agency_id,
        "knowable_at": AS_OF - timedelta(days=days_ago),
    }
    if cutoff is not None:
        out["cutoff"] = cutoff
    return out


def features(
    *,
    vendor_id: int = 1,
    relevant: int = 5,
    activity_affinity: float = 0.5,
    agency_affinity: float = 0.5,
    recency: float = 1.0,
    base_rate: float = 0.02,
) -> ParticipationFeatures:
    """A feature vector with every dimension explicitly pinned."""
    return ParticipationFeatures(
        vendor_id=vendor_id,
        total_count=max(relevant, 1),
        activity_count=relevant,
        agency_count=relevant,
        relevant_count=relevant,
        activity_affinity=activity_affinity,
        agency_affinity=agency_affinity,
        recency=recency,
        days_since_last=0.0,
        base_rate=base_rate,
    )


# --------------------------------------------------------------------------
# Pure maths
# --------------------------------------------------------------------------

def test_logistic_is_bounded_and_saturates_without_overflowing():
    for z in (-1e9, -1000.0, -5.0, 0.0, 5.0, 1000.0, 1e9):
        p = logistic(z)
        assert 0.0 <= p <= 1.0
    assert logistic(0.0) == pytest.approx(0.5)
    assert logistic(-1e9) == pytest.approx(0.0, abs=1e-12)
    assert logistic(1e9) == pytest.approx(1.0, abs=1e-12)


def test_logit_is_finite_at_the_degenerate_endpoints():
    assert logit(0.0) < -18.0
    assert logit(1.0) > 18.0
    assert logit(0.5) == pytest.approx(0.0)
    # Round trip through the pair is stable in the interior.
    assert logistic(logit(0.137)) == pytest.approx(0.137)


def test_logistic_and_logit_reject_nan():
    with pytest.raises(ValueError):
        logistic(float("nan"))
    with pytest.raises(ValueError):
        logit(float("nan"))


# --------------------------------------------------------------------------
# Probability bounds and monotonicity
# --------------------------------------------------------------------------

def test_probability_stays_in_unit_interval_under_extreme_inputs():
    """Absurd features and absurd coefficients must still yield a probability."""
    extremes = [
        features(activity_affinity=0.0, agency_affinity=0.0, recency=0.0, base_rate=0.0),
        features(activity_affinity=1.0, agency_affinity=1.0, recency=1.0, base_rate=1.0),
        features(activity_affinity=1e6, agency_affinity=1e6, recency=1e6, base_rate=0.999),
        features(activity_affinity=-1e6, agency_affinity=-1e6, recency=-1e6, base_rate=1e-12),
    ]
    coefficient_sets = [
        None,
        dict(PARTICIPATION_COEFFICIENTS, activity_affinity=1e6),
        dict(PARTICIPATION_COEFFICIENTS, activity_affinity=-1e6, base_rate=-1e6),
        {k: 0.0 for k in PARTICIPATION_COEFFICIENTS},
    ]
    for feat in extremes:
        for coeff in coefficient_sets:
            est = estimate_from_features(feat, coefficients=coeff)
            assert est.probability is not None
            assert PROBABILITY_FLOOR <= est.probability <= PROBABILITY_CEILING
            assert 0.0 <= est.probability <= 1.0


def test_probability_never_reaches_certainty():
    """House rule 2: no wording or number may imply a competitor will bid."""
    est = estimate_from_features(
        features(activity_affinity=1.0, agency_affinity=1.0, recency=1.0, base_rate=0.5),
        coefficients=dict(PARTICIPATION_COEFFICIENTS, activity_affinity=500.0),
    )
    assert est.probability == PROBABILITY_CEILING
    assert est.probability < 1.0


def test_more_activity_affinity_means_higher_probability_all_else_equal():
    probs = [
        estimate_from_features(features(activity_affinity=a)).probability
        for a in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0)
    ]
    assert all(p is not None for p in probs)
    assert probs == sorted(probs)
    assert probs[-1] > probs[0]


def test_agency_affinity_and_recency_are_also_monotone_increasing():
    by_agency = [
        estimate_from_features(features(agency_affinity=a)).probability for a in (0.0, 0.5, 1.0)
    ]
    by_recency = [
        estimate_from_features(features(recency=r)).probability for r in (0.0, 0.5, 1.0)
    ]
    assert by_agency == sorted(by_agency) and by_agency[-1] > by_agency[0]
    assert by_recency == sorted(by_recency) and by_recency[-1] > by_recency[0]


def test_higher_market_base_rate_raises_the_prior():
    low = estimate_from_features(features(base_rate=0.01)).probability
    high = estimate_from_features(features(base_rate=0.2)).probability
    assert high > low


# --------------------------------------------------------------------------
# Suppression
# --------------------------------------------------------------------------

def test_zero_evidence_vendor_is_suppressed_not_scored_at_half():
    est = estimate_from_features(
        ParticipationFeatures(
            vendor_id=7,
            total_count=4,
            activity_count=0,
            agency_count=0,
            relevant_count=0,
            activity_affinity=0.0,
            agency_affinity=0.0,
            recency=1.0,
            days_since_last=1.0,
            base_rate=DEFAULT_BASE_RATE,
        )
    )
    assert est.probability is None
    assert est.suppression is SuppressionReason.INSUFFICIENT_EVIDENCE
    assert est.basis == BASIS_NO_EVIDENCE
    assert est.evidence_count == 0
    assert est.is_suppressed


@pytest.mark.parametrize("relevant", list(range(MIN_EVIDENCE_FOR_PREDICTION)))
def test_thin_evidence_is_suppressed_below_the_threshold(relevant):
    est = estimate_from_features(features(relevant=relevant, activity_affinity=1.0))
    assert est.probability is None
    assert est.suppression is SuppressionReason.INSUFFICIENT_EVIDENCE
    assert est.basis == (BASIS_NO_EVIDENCE if relevant == 0 else BASIS_INSUFFICIENT)


def test_evidence_at_the_threshold_is_scored():
    est = estimate_from_features(features(relevant=MIN_EVIDENCE_FOR_PREDICTION))
    assert est.suppression is None
    assert est.probability is not None


def test_estimate_rejects_contradictory_states():
    with pytest.raises(ValueError):
        ParticipationEstimate(
            vendor_id=1,
            probability=0.3,
            evidence_count=5,
            basis=BASIS_ACTIVITY,
            suppression=SuppressionReason.INSUFFICIENT_EVIDENCE,
        )
    with pytest.raises(ValueError):
        ParticipationEstimate(
            vendor_id=1,
            probability=None,
            evidence_count=5,
            basis=BASIS_ACTIVITY,
            suppression=None,
        )
    with pytest.raises(ValueError):
        ParticipationEstimate(
            vendor_id=1,
            probability=1.4,
            evidence_count=5,
            basis=BASIS_ACTIVITY,
            suppression=None,
        )


def test_estimate_to_dict_is_json_shaped():
    payload = estimate_from_features(features()).to_dict()
    assert payload["model_version"] == MODEL_VERSION
    assert payload["is_suppressed"] is False
    assert payload["suppression"] is None
    assert {f["kind"] for f in payload["factors"]} <= {"observed", "derived", "predicted"}
    # House rule 1: the observed count is tagged observed, the score's inputs derived.
    kinds = {f["name"]: f["kind"] for f in payload["factors"]}
    assert kinds["relevant_prior_offers"] == "observed"
    assert kinds["activity_affinity"] == "derived"


# --------------------------------------------------------------------------
# Feature construction
# --------------------------------------------------------------------------

def test_build_features_computes_shares_counts_and_recency():
    rows = [
        row(1, tender_id=1, days_ago=1.0),
        row(1, tender_id=2, days_ago=1.0),
        row(1, tender_id=3, activity_id=999, agency_id=999, days_ago=1.0),
    ]
    feats = build_features(
        rows, activity_id=ACTIVITY, agency_id=AGENCY, as_of=AS_OF, base_rate=0.02
    )[1]
    assert feats.total_count == 3
    assert feats.activity_count == 2
    assert feats.agency_count == 2
    assert feats.relevant_count == 2
    assert feats.activity_affinity == pytest.approx(2 / 3)
    assert feats.recency > 0.99  # a day old: 0.5 ** (1/365)
    assert feats.days_since_last == pytest.approx(1.0)


def test_build_features_applies_the_recency_half_life():
    rows = [row(1, tender_id=1, days_ago=HALF_LIFE_DAYS)]
    feats = build_features(
        rows, activity_id=ACTIVITY, agency_id=AGENCY, as_of=AS_OF, base_rate=0.02
    )[1]
    assert feats.recency == pytest.approx(0.5)
    # A single row is still 100% of that vendor's weighted history.
    assert feats.activity_affinity == pytest.approx(1.0)


def test_build_features_drops_rows_dated_at_or_after_as_of():
    """Point-in-time gate: a fact from the future may not enter a feature."""
    rows = [
        row(1, tender_id=1, days_ago=10.0),
        row(1, tender_id=2, days_ago=-5.0),   # after as_of
        row(1, tender_id=3, days_ago=0.0),    # exactly at as_of
    ]
    feats = build_features(
        rows, activity_id=ACTIVITY, agency_id=AGENCY, as_of=AS_OF, base_rate=0.02
    )[1]
    assert feats.total_count == 1


def test_build_features_with_recency_decay_weights_a_stale_activity_bid_less():
    fresh = build_features(
        [row(1, tender_id=1, days_ago=1.0), row(1, tender_id=2, activity_id=999, days_ago=700.0)],
        activity_id=ACTIVITY,
        agency_id=None,
        as_of=AS_OF,
        base_rate=0.02,
    )[1]
    stale = build_features(
        [row(1, tender_id=1, days_ago=700.0), row(1, tender_id=2, activity_id=999, days_ago=1.0)],
        activity_id=ACTIVITY,
        agency_id=None,
        as_of=AS_OF,
        base_rate=0.02,
    )[1]
    assert fresh.activity_affinity > stale.activity_affinity


def test_normalise_history_rows_drops_unattributable_or_undated_rows():
    rows = normalise_history_rows(
        [
            row(1, tender_id=1),
            {**row(2, tender_id=2), "vendor_id": None},
            {**row(3, tender_id=3), "knowable_at": None},
        ]
    )
    assert [r["vendor_id"] for r in rows] == [1]
    assert rows[0]["knowable_at"].tzinfo is not None


def test_normalise_history_rows_treats_naive_timestamps_as_utc():
    naive_at = datetime(2026, 1, 1, 9, 0)  # noqa: DTZ001 - the naive input is the point of this test
    naive = {**row(1, tender_id=1), "knowable_at": naive_at}
    out = normalise_history_rows([naive])
    assert out[0]["knowable_at"] == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Base rate
# --------------------------------------------------------------------------

def test_base_rate_falls_back_when_the_activity_is_too_thin():
    rows = [row(v, tender_id=v) for v in range(1, MIN_BASE_RATE_TENDERS)]
    rate, tenders = base_rate_from_rows(rows, activity_id=ACTIVITY)
    assert rate == DEFAULT_BASE_RATE
    assert tenders == 0


def test_base_rate_is_measured_when_the_activity_is_thick_enough():
    # 6 tenders, 6 vendors, one offer each -> 6 / 36.
    rows = [row(v, tender_id=v) for v in range(1, 7)]
    rate, tenders = base_rate_from_rows(rows, activity_id=ACTIVITY)
    assert tenders == 6
    assert rate == pytest.approx(6 / 36)


def test_base_rate_ignores_rows_from_other_activities():
    rows = [row(v, tender_id=v) for v in range(1, 7)]
    rows += [row(v, tender_id=100 + v, activity_id=999) for v in range(50, 80)]
    rate, tenders = base_rate_from_rows(rows, activity_id=ACTIVITY)
    assert tenders == 6
    assert rate == pytest.approx(6 / 36)


def test_base_rate_is_clamped_into_a_usable_band():
    # 6 tenders, 1 vendor, bidding on all of them -> raw rate 1.0.
    rows = [row(1, tender_id=t) for t in range(1, 7)]
    rate, _ = base_rate_from_rows(rows, activity_id=ACTIVITY)
    assert rate <= 0.5
    assert 0.0 < rate < 1.0


# --------------------------------------------------------------------------
# as_of resolution
# --------------------------------------------------------------------------

def test_resolve_as_of_prefers_explicit_then_opening_then_last_offer():
    opening = datetime(2026, 3, 1, tzinfo=UTC)
    last = datetime(2026, 2, 1, tzinfo=UTC)
    assert resolve_as_of({"offers_opening_date": opening}, AS_OF) == AS_OF
    assert resolve_as_of({"offers_opening_date": opening, "last_offer_date": last}) == opening
    assert resolve_as_of({"last_offer_date": last}) == last
    assert resolve_as_of({}).tzinfo is UTC


# --------------------------------------------------------------------------
# DB entry points, against a fake connection
# --------------------------------------------------------------------------

async def test_candidate_bidders_ranks_scored_first_then_suppressed():
    rows = (
        # vendor 1: 4 prior activity bids -> scored
        [row(1, tender_id=t) for t in range(1, 5)]
        # vendor 2: 3 bids but only 1 relevant -> suppressed
        + [row(2, tender_id=10)]
        + [row(2, tender_id=t, activity_id=999, agency_id=999) for t in (11, 12)]
        # vendor 3: 6 relevant bids, purely in-activity -> scored, and higher
        + [row(3, tender_id=t) for t in range(20, 26)]
    )
    conn = FakeConn(rows)
    out = await candidate_bidders(conn, tender=TENDER, as_of=AS_OF)

    assert [e.vendor_id for e in out][:2] == [3, 1] or [e.vendor_id for e in out][:2] == [1, 3]
    scored = [e for e in out if e.probability is not None]
    suppressed = [e for e in out if e.probability is None]
    assert {e.vendor_id for e in scored} == {1, 3}
    assert [e.vendor_id for e in suppressed] == [2]
    # suppressed candidates sort last
    assert out[-1].vendor_id == 2
    assert out[-1].suppression is SuppressionReason.INSUFFICIENT_EVIDENCE


async def test_candidate_bidders_is_deterministic_and_respects_limit():
    rows = [row(v, tender_id=100 * v + i) for v in range(1, 8) for i in range(4)]
    first = await candidate_bidders(FakeConn(rows), tender=TENDER, as_of=AS_OF, limit=3)
    second = await candidate_bidders(FakeConn(rows), tender=TENDER, as_of=AS_OF, limit=3)
    assert len(first) == 3
    assert [e.vendor_id for e in first] == [e.vendor_id for e in second]
    assert [e.probability for e in first] == [e.probability for e in second]


async def test_candidate_bidders_passes_the_point_in_time_cutoff_to_sql():
    conn = FakeConn([])
    await candidate_bidders(conn, tender=TENDER, as_of=AS_OF)
    query, args = conn.calls[0]
    assert args == (TENDER["id"], AS_OF, ACTIVITY, AGENCY)
    assert "COALESCE(a.awarded_at, a.created_at) < $2" in query


async def test_candidate_bidders_without_activity_or_agency_returns_nothing():
    conn = FakeConn([row(1, tender_id=1)])
    out = await candidate_bidders(conn, tender={"id": 1}, as_of=AS_OF)
    assert out == []
    assert conn.calls == []  # no pointless query


async def test_candidate_bidders_with_no_history_returns_empty():
    out = await candidate_bidders(FakeConn([]), tender=TENDER, as_of=AS_OF)
    assert out == []


async def test_candidate_bidders_limit_zero_is_a_no_op():
    conn = FakeConn([row(1, tender_id=1)])
    assert await candidate_bidders(conn, tender=TENDER, as_of=AS_OF, limit=0) == []
    assert conn.calls == []


async def test_participation_probability_scores_a_known_vendor():
    rows = [row(4, tender_id=t) for t in range(1, 6)]
    est = await participation_probability(
        FakeConn(rows), vendor_id=4, tender=TENDER, as_of=AS_OF
    )
    assert est.vendor_id == 4
    assert est.suppression is None
    assert est.probability is not None
    assert 0.0 < est.probability < 1.0
    assert est.evidence_count == 5
    assert est.basis == BASIS_ACTIVITY_AND_AGENCY
    assert est.model_version == MODEL_VERSION
    assert est.calibrated is False  # nothing has validated these coefficients


async def test_participation_probability_for_an_unknown_vendor_is_suppressed():
    est = await participation_probability(
        FakeConn([]), vendor_id=999, tender=TENDER, as_of=AS_OF
    )
    assert est.probability is None
    assert est.suppression is SuppressionReason.INSUFFICIENT_EVIDENCE
    assert est.evidence_count == 0
    assert est.basis == BASIS_NO_EVIDENCE


async def test_participation_probability_agency_only_history():
    rows = [row(4, tender_id=t, activity_id=999) for t in range(1, 5)]
    est = await participation_probability(
        FakeConn(rows), vendor_id=4, tender=TENDER, as_of=AS_OF
    )
    assert est.basis == BASIS_AGENCY
    assert est.features is not None
    assert est.features.activity_affinity == pytest.approx(0.0)
    assert est.features.agency_affinity == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------

def test_brier_score_matches_hand_computed_values():
    assert brier_score([(1.0, True), (0.0, False)]) == pytest.approx(0.0)
    assert brier_score([(0.5, True), (0.5, False)]) == pytest.approx(0.25)
    assert brier_score([(0.2, True)]) == pytest.approx(0.64)


def test_brier_score_over_nothing_is_undefined_not_perfect():
    with pytest.raises(ValueError):
        brier_score([])


def test_reliability_buckets_cover_the_whole_range_including_empty_bands():
    events = [(0.05, False), (0.05, True), (0.95, True), (1.0, True)]
    out = reliability_buckets(events, buckets=10)
    assert len(out) == 10
    assert out[0]["count"] == 2
    assert out[0]["observed_rate"] == pytest.approx(0.5)
    assert out[-1]["count"] == 2  # 0.95 and the closed-right 1.0
    assert out[5]["count"] == 0
    assert out[5]["observed_rate"] is None
    assert sum(b["count"] for b in out) == len(events)


def test_reliability_buckets_rejects_a_nonsense_bucket_count():
    with pytest.raises(ValueError):
        reliability_buckets([(0.5, True)], buckets=0)


def test_summarise_calibration_refuses_metrics_on_a_tiny_sample():
    events = [(0.1, False)] * (MIN_CALIBRATION_EVENTS - 1)
    report = summarise_calibration(events)
    assert report["status"] == "insufficient_data"
    assert report["events"] == MIN_CALIBRATION_EVENTS - 1
    assert report["suppression_reason"] == SuppressionReason.INSUFFICIENT_EVIDENCE.value
    # No metric may be present at all — a Brier score from 49 events looks like
    # a measurement and is not one.
    assert "brier_score" not in report
    assert "reliability_buckets" not in report


def test_summarise_calibration_reports_metrics_once_the_sample_is_large_enough():
    events = [(0.2, i % 5 == 0) for i in range(MIN_CALIBRATION_EVENTS)]
    report = summarise_calibration(events)
    assert report["status"] == "ok"
    assert report["brier_score"] == pytest.approx(brier_score(events))
    assert report["observed_rate"] == pytest.approx(0.2)
    assert report["mean_predicted"] == pytest.approx(0.2)
    assert len(report["reliability_buckets"]) == 10
    assert report["model_version"] == MODEL_VERSION
    assert report["coefficients"] == PARTICIPATION_COEFFICIENTS


async def test_calibrate_returns_insufficient_data_on_a_tiny_fixture():
    cutoff = AS_OF
    rows = [
        row(1, tender_id=1, days_ago=100.0, cutoff=cutoff - timedelta(days=90)),
        row(2, tender_id=1, days_ago=100.0, cutoff=cutoff - timedelta(days=90)),
        row(1, tender_id=2, days_ago=10.0, cutoff=cutoff - timedelta(days=5)),
    ]
    report = await calibrate(FakeConn(rows), as_of=AS_OF)
    assert report["status"] == "insufficient_data"
    assert report["events"] < MIN_CALIBRATION_EVENTS
    assert report["min_events"] == MIN_CALIBRATION_EVENTS
    assert "brier_score" not in report


async def test_calibrate_on_an_empty_corpus_explains_the_emptiness():
    report = await calibrate(FakeConn([]), as_of=AS_OF)
    assert report["status"] == "insufficient_data"
    assert report["events"] == 0
    assert "note" in report


async def test_calibrate_builds_labelled_events_when_history_is_knowable_in_time():
    """A synthetic corpus where prior bids ARE knowable before later cutoffs.

    This is the shape the real corpus will have once award dates land; today it
    exists only in this fixture, which is exactly why calibrate() suppresses
    against live data.
    """
    rows: list[dict] = []
    base = AS_OF - timedelta(days=800)
    # Five early tenders each with three repeat bidders, knowable early.
    for t in range(1, 6):
        moment = base + timedelta(days=t)
        for v in (1, 2, 3):
            rows.append(
                {
                    "vendor_id": v,
                    "tender_id": t,
                    "activity_id": ACTIVITY,
                    "agency_id": AGENCY,
                    "knowable_at": moment,
                    "cutoff": moment,
                }
            )
    # A later tender whose cutoff sits after all of the above.
    late = AS_OF - timedelta(days=10)
    for v in (1, 2):
        rows.append(
            {
                "vendor_id": v,
                "tender_id": 99,
                "activity_id": ACTIVITY,
                "agency_id": AGENCY,
                "knowable_at": late,
                "cutoff": late,
            }
        )
    report = await calibrate(FakeConn(rows), as_of=AS_OF, min_events=1)
    assert report["status"] == "ok"
    assert report["events"] >= 3
    assert 0.0 <= report["brier_score"] <= 1.0
    assert 0.0 <= report["observed_rate"] <= 1.0
    assert report["tenders_scanned"] >= 6


async def test_calibrate_is_reproducible_for_a_fixed_corpus_and_as_of():
    rows = [
        {
            "vendor_id": v,
            "tender_id": t,
            "activity_id": ACTIVITY,
            "agency_id": AGENCY,
            "knowable_at": AS_OF - timedelta(days=800 - t),
            "cutoff": AS_OF - timedelta(days=800 - t),
        }
        for t in range(1, 8)
        for v in (1, 2, 3)
    ]
    a = await calibrate(FakeConn(list(rows)), as_of=AS_OF, min_events=1)
    b = await calibrate(FakeConn(list(rows)), as_of=AS_OF, min_events=1)
    assert a == b


# --------------------------------------------------------------------------
# Optional: read-only sanity checks against the live database.
# --------------------------------------------------------------------------

LIVE_DSN = os.environ.get(
    "THAQIP_TEST_DSN", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip"
)


async def _live_conn():
    asyncpg = pytest.importorskip("asyncpg")
    try:
        return await asyncpg.connect(LIVE_DSN, timeout=3)
    except Exception as exc:  # noqa: BLE001 - any connect failure means "skip"  # pragma: no cover
        pytest.skip(f"live database not reachable: {exc}")


@pytest.mark.asyncio
async def test_live_candidate_bidders_runs_and_stays_in_bounds():
    conn = await _live_conn()
    try:
        record = await conn.fetchrow(
            "SELECT t.id, t.activity_id, t.agency_id "
            "FROM tenders t JOIN offers o ON o.tender_id = t.id "
            "WHERE t.activity_id IS NOT NULL GROUP BY t.id LIMIT 1"
        )
        if record is None:  # pragma: no cover - depends on corpus
            pytest.skip("no tender with offers and an activity")
        tender = dict(record)
        out = await candidate_bidders(conn, tender=tender, as_of=datetime.now(UTC))
        assert len(out) <= 15
        for est in out:
            if est.probability is None:
                assert est.suppression is SuppressionReason.INSUFFICIENT_EVIDENCE
            else:
                assert PROBABILITY_FLOOR <= est.probability <= PROBABILITY_CEILING
                assert est.evidence_count >= MIN_EVIDENCE_FOR_PREDICTION
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_live_calibrate_reports_a_self_consistent_report():
    """Asserts the report's internal consistency, never a target metric.

    Pinning "Brier below X" here would turn a measurement into a goal and
    reward tuning the coefficients until the test passes. The only things
    asserted are invariants that must hold whatever the corpus says.
    """
    conn = await _live_conn()
    try:
        report = await calibrate(conn, as_of=datetime.now(UTC))
        assert report["status"] in {"ok", "insufficient_data"}
        if report["status"] == "insufficient_data":
            assert "brier_score" not in report
            assert "reliability_buckets" not in report
        else:
            assert report["events"] >= MIN_CALIBRATION_EVENTS
            assert 0.0 <= report["brier_score"] <= 1.0
            assert 0.0 <= report["observed_rate"] <= 1.0
            assert sum(b["count"] for b in report["reliability_buckets"]) == report["events"]
    finally:
        await conn.close()


async def test_calibrate_skips_tenders_with_no_real_decision_moment():
    """A NULL cutoff must not be back-filled from the ingestion timestamp.

    Doing so would order the corpus by crawl time and manufacture calibration
    events out of it, which is exactly the leakage the point-in-time rule bars.
    """
    rows = [
        {
            "vendor_id": v,
            "tender_id": t,
            "activity_id": ACTIVITY,
            "agency_id": AGENCY,
            "knowable_at": AS_OF - timedelta(days=800 - t),
            "cutoff": None,
        }
        for t in range(1, 20)
        for v in (1, 2, 3)
    ]
    report = await calibrate(FakeConn(rows), as_of=AS_OF, min_events=1)
    assert report["tenders_scanned"] == 0
    assert report["events"] == 0
    assert report["status"] == "insufficient_data"
