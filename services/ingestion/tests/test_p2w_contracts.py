"""Unit tests for the P2W shared contracts.

Focus is the suppression invariant in BOTH directions, because that is the rule
that keeps the product from inventing a competitor price out of two data points.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from thaqip_ingestion.p2w.contracts import (
    FRESHNESS_HORIZON_DAYS,
    MODEL_VERSION,
    EvidenceTier,
    ExplanationFactor,
    PredictionScope,
    PricePrediction,
    Quantiles,
    SuppressionReason,
    freshness_score,
)

# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

def test_model_version_constant():
    assert MODEL_VERSION == "p2w-0.1.0"


def test_evidence_tier_values_and_str_behaviour():
    assert [t.value for t in EvidenceTier] == ["A", "B", "C", "D"]
    assert EvidenceTier("A") is EvidenceTier.A
    # str-Enum: usable directly as a DB/JSON value
    assert EvidenceTier.C == "C"
    assert json.dumps({"tier": EvidenceTier.B}) == '{"tier": "B"}'


def test_only_tiers_a_and_b_allow_competitor_predictions():
    assert EvidenceTier.A.allows_competitor_prediction
    assert EvidenceTier.B.allows_competitor_prediction
    assert not EvidenceTier.C.allows_competitor_prediction
    assert not EvidenceTier.D.allows_competitor_prediction


def test_tier_b_requires_widened_interval_and_tier_d_has_no_market_price():
    assert EvidenceTier.B.requires_widened_interval
    assert not EvidenceTier.A.requires_widened_interval
    assert EvidenceTier.C.allows_market_prediction
    assert not EvidenceTier.D.allows_market_prediction


def test_prediction_scope_values():
    assert {s.value for s in PredictionScope} == {"MARKET", "COMPETITOR", "USER_OPTIMIZER"}


def test_suppression_reason_values_cover_the_spec_set():
    assert {r.value for r in SuppressionReason} == {
        "INSUFFICIENT_EVIDENCE", "STALE_DATA", "LOW_SIMILARITY", "NO_COMPARABLE_TENDERS",
        "TENANT_DATA_UNAVAILABLE", "MODEL_UNAVAILABLE", "CALIBRATION_FAILED",
    }


# --------------------------------------------------------------------------
# Quantiles
# --------------------------------------------------------------------------

def test_quantiles_validate_accepts_ordered_and_returns_self():
    q = Quantiles(1.0, 2.0, 3.0)
    assert q.validate() is q


def test_quantiles_validate_accepts_degenerate_equal_triple():
    # A single observation collapses the interval; that is legal, if uninformative.
    assert Quantiles(5.0, 5.0, 5.0).validate().width == 0.0


@pytest.mark.parametrize("p10,p50,p90", [
    (3.0, 2.0, 1.0),   # fully reversed
    (1.0, 3.0, 2.0),   # p50 above p90
    (2.0, 1.0, 3.0),   # p50 below p10
])
def test_quantiles_validate_rejects_unordered(p10, p50, p90):
    with pytest.raises(ValueError, match="ordered"):
        Quantiles(p10, p50, p90).validate()


def test_quantiles_validate_rejects_none_and_nan():
    with pytest.raises(ValueError, match="p50"):
        Quantiles(1.0, None, 3.0).validate()
    with pytest.raises(ValueError, match="NaN"):
        Quantiles(1.0, float("nan"), 3.0).validate()


def test_quantiles_width_and_to_dict():
    q = Quantiles(100.0, 150.0, 400.0)
    assert q.width == 300.0
    assert q.to_dict() == {"p10": 100.0, "p50": 150.0, "p90": 400.0}


# --------------------------------------------------------------------------
# freshness_score
# --------------------------------------------------------------------------

def test_freshness_score_endpoints_and_clamping():
    assert freshness_score(0) == 100
    assert freshness_score(-10) == 100          # future-dated evidence clamps, never exceeds
    assert freshness_score(FRESHNESS_HORIZON_DAYS) == 0
    assert freshness_score(5000) == 0           # beyond the horizon stays at the floor


def test_freshness_score_decays_monotonically():
    scores = [freshness_score(d) for d in (0, 30, 180, 365, 700, 730)]
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= s <= 100 for s in scores)


def test_freshness_score_midpoint_is_about_half():
    assert freshness_score(365) == 50


def test_freshness_score_rejects_nan():
    with pytest.raises(ValueError):
        freshness_score(float("nan"))


# --------------------------------------------------------------------------
# ExplanationFactor
# --------------------------------------------------------------------------

def test_explanation_factor_roundtrips_to_dict():
    f = ExplanationFactor(
        name="bidder_count", direction="increases", weight=0.4, kind="observed",
        detail="7 مقدمي عروض", evidence_ref="tenders:1042",
    )
    assert f.to_dict() == {
        "name": "bidder_count", "direction": "increases", "weight": 0.4,
        "kind": "observed", "detail": "7 مقدمي عروض", "evidence_ref": "tenders:1042",
    }


def test_explanation_factor_rejects_bad_direction_and_kind():
    with pytest.raises(ValueError, match="direction"):
        ExplanationFactor(name="x", direction="up", weight=1.0, kind="observed")
    with pytest.raises(ValueError, match="kind"):
        ExplanationFactor(name="x", direction="neutral", weight=1.0, kind="guessed")


def test_explanation_factor_kinds_keep_observed_and_predicted_distinct():
    for kind in ("observed", "derived", "predicted"):
        assert ExplanationFactor(name="k", direction="neutral", weight=0,
                                 kind=kind).kind == kind


def test_explanation_factor_requires_name_and_numeric_weight():
    with pytest.raises(ValueError, match="name"):
        ExplanationFactor(name="", direction="neutral", weight=1.0, kind="derived")
    with pytest.raises(ValueError, match="numeric"):
        ExplanationFactor(name="x", direction="neutral", weight="heavy", kind="derived")


# --------------------------------------------------------------------------
# PricePrediction — the suppression invariant, both directions
# --------------------------------------------------------------------------

def _valid_kwargs(**over):
    base = dict(
        tender_id=1, prediction_scope=PredictionScope.MARKET,
        p10=90.0, p50=100.0, p90=130.0,
    )
    base.update(over)
    return base


def test_unsuppressed_prediction_with_ordered_quantiles_is_accepted():
    p = PricePrediction(**_valid_kwargs(evidence_count=12, evidence_tier=EvidenceTier.A))
    assert not p.is_suppressed
    assert p.quantiles == Quantiles(90.0, 100.0, 130.0)
    assert p.model_version == MODEL_VERSION


@pytest.mark.parametrize("missing", ["p10", "p50", "p90"])
def test_unsuppressed_prediction_missing_a_quantile_raises(missing):
    kwargs = _valid_kwargs(**{missing: None})
    with pytest.raises(ValueError) as exc:
        PricePrediction(**kwargs)
    assert missing in str(exc.value)
    assert "suppression_reason" in str(exc.value)


def test_unsuppressed_prediction_with_unordered_quantiles_raises():
    with pytest.raises(ValueError, match="ordered"):
        PricePrediction(**_valid_kwargs(p10=200.0, p50=100.0, p90=130.0))


@pytest.mark.parametrize("field_name,value", [
    ("p10", 90.0), ("p50", 100.0), ("p90", 130.0), ("win_probability", 0.5),
])
def test_suppressed_prediction_carrying_any_number_raises(field_name, value):
    kwargs = dict(
        tender_id=1, prediction_scope=PredictionScope.COMPETITOR,
        suppression_reason=SuppressionReason.INSUFFICIENT_EVIDENCE,
    )
    kwargs[field_name] = value
    with pytest.raises(ValueError) as exc:
        PricePrediction(**kwargs)
    assert "suppressed" in str(exc.value)
    assert field_name in str(exc.value)


def test_suppressed_classmethod_produces_all_none_numerics():
    p = PricePrediction.suppressed(
        tender_id=42, scope=PredictionScope.COMPETITOR,
        reason=SuppressionReason.INSUFFICIENT_EVIDENCE,
        subject_id=7, evidence_count=2, evidence_tier=EvidenceTier.C,
    )
    assert p.is_suppressed
    assert p.quantiles is None
    assert (p.p10, p.p50, p.p90, p.expected_value, p.win_probability) == (
        None, None, None, None, None)
    assert p.suppression_reason is SuppressionReason.INSUFFICIENT_EVIDENCE
    # the evidence that justified the silence still travels with it
    assert p.evidence_count == 2
    assert p.evidence_tier is EvidenceTier.C
    assert p.subject_id == 7


def test_suppressed_classmethod_keeps_explanation_factors():
    factors = [ExplanationFactor(name="observations", direction="neutral",
                                 weight=1.0, kind="observed", detail="عرضان فقط")]
    p = PricePrediction.suppressed(
        1, PredictionScope.COMPETITOR, SuppressionReason.INSUFFICIENT_EVIDENCE,
        explanation_factors=factors,
    )
    assert [f.name for f in p.explanation_factors] == ["observations"]


def test_suppressed_accepts_plain_strings_for_scope_and_reason():
    p = PricePrediction.suppressed(1, "MARKET", "NO_COMPARABLE_TENDERS")
    assert p.prediction_scope is PredictionScope.MARKET
    assert p.suppression_reason is SuppressionReason.NO_COMPARABLE_TENDERS


def test_unknown_enum_string_is_rejected():
    with pytest.raises(ValueError):
        PricePrediction(**_valid_kwargs(prediction_scope="GUESS"))
    with pytest.raises(ValueError):
        PricePrediction.suppressed(1, PredictionScope.MARKET, "BECAUSE_I_SAID_SO")


# --------------------------------------------------------------------------
# PricePrediction — remaining field validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prob", [-0.01, 1.01, 5.0])
def test_win_probability_outside_unit_interval_raises(prob):
    with pytest.raises(ValueError, match="win_probability"):
        PricePrediction(**_valid_kwargs(win_probability=prob))


def test_win_probability_boundaries_accepted():
    for prob in (0.0, 1.0):
        assert PricePrediction(**_valid_kwargs(win_probability=prob)).win_probability == prob


@pytest.mark.parametrize("name", [
    "confidence_score", "similarity_confidence", "data_freshness_score",
])
def test_score_fields_are_bounded_0_100(name):
    with pytest.raises(ValueError, match=name):
        PricePrediction(**_valid_kwargs(**{name: 101}))
    with pytest.raises(ValueError, match=name):
        PricePrediction(**_valid_kwargs(**{name: -1}))
    assert getattr(PricePrediction(**_valid_kwargs(**{name: 0})), name) == 0
    assert getattr(PricePrediction(**_valid_kwargs(**{name: 100})), name) == 100


def test_confidence_score_is_not_win_probability():
    # They are separate fields with separate ranges; a 90 confidence must not be
    # readable as a 0.9 win probability.
    p = PricePrediction(**_valid_kwargs(confidence_score=90, win_probability=0.2))
    assert p.confidence_score == 90
    assert p.win_probability == 0.2


def test_negative_evidence_count_and_empty_model_version_raise():
    with pytest.raises(ValueError, match="evidence_count"):
        PricePrediction(**_valid_kwargs(evidence_count=-1))
    with pytest.raises(ValueError, match="model_version"):
        PricePrediction(**_valid_kwargs(model_version=""))


# --------------------------------------------------------------------------
# to_dict
# --------------------------------------------------------------------------

def test_to_dict_is_json_serialisable_and_complete():
    p = PricePrediction(
        tender_id=9, prediction_scope=PredictionScope.USER_OPTIMIZER, subject_id=None,
        p10=1000.0, p50=1200.0, p90=1500.0, expected_value=1210.0, win_probability=0.35,
        confidence_score=61, similarity_confidence=48, data_freshness_score=88,
        evidence_count=17, evidence_tier=EvidenceTier.B, feature_snapshot_id="snap-1",
        seed=1234, generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        explanation_factors=[ExplanationFactor(name="agency_history",
                                               direction="decreases", weight=0.3,
                                               kind="derived")],
    )
    d = p.to_dict()
    json.dumps(d)  # must not raise
    assert d["prediction_scope"] == "USER_OPTIMIZER"
    assert d["evidence_tier"] == "B"
    assert d["suppression_reason"] is None
    assert d["is_suppressed"] is False
    assert d["generated_at"] == "2026-01-02T03:04:05+00:00"
    assert d["explanation_factors"][0]["kind"] == "derived"
    expected_keys = {
        "tender_id", "prediction_scope", "subject_id", "p10", "p50", "p90",
        "expected_value", "win_probability", "confidence_score", "similarity_confidence",
        "data_freshness_score", "evidence_count", "evidence_tier", "model_version",
        "feature_snapshot_id", "seed", "generated_at", "explanation_factors",
        "suppression_reason", "is_suppressed",
    }
    assert set(d) == expected_keys


def test_to_dict_of_suppressed_prediction_has_null_numbers_and_a_reason():
    d = PricePrediction.suppressed(
        3, PredictionScope.COMPETITOR, SuppressionReason.LOW_SIMILARITY,
    ).to_dict()
    json.dumps(d)
    assert d["suppression_reason"] == "LOW_SIMILARITY"
    assert d["is_suppressed"] is True
    assert d["p10"] is d["p50"] is d["p90"] is d["win_probability"] is None


def test_generated_at_defaults_to_timezone_aware_utc():
    p = PricePrediction(**_valid_kwargs())
    assert p.generated_at.tzinfo is not None
    assert p.generated_at.utcoffset().total_seconds() == 0


def test_package_reexports_contracts():
    from thaqip_ingestion import p2w

    assert p2w.MODEL_VERSION == MODEL_VERSION
    assert p2w.EvidenceTier is EvidenceTier
    assert p2w.PricePrediction is PricePrediction
