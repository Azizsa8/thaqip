"""Tests for the deterministic eligibility/fit scoring engine
(thaqip_ingestion.fit_score), covering PRD tests T-ELIG-01/T-ELIG-02.
"""
from __future__ import annotations

from dataclasses import dataclass

from thaqip_ingestion.fit_score import (
    CompanyProfile,
    TenderDetailLike,
    score_fit,
)


@dataclass
class Tender:
    activity_id: int | None


def test_no_profile_and_no_details_is_unconfirmed_not_eligible():
    # T-ELIG-02: a tender without a details-report fetch, and no company
    # profile at all, must never be labelled as eligible.
    result = score_fit(Tender(activity_id=111), CompanyProfile())
    assert result.classification_status == "unconfirmed"
    assert result.label == "غير مؤكد التصنيف"
    assert result.score == 0
    assert result.max_possible_score == 0


def test_missing_details_report_is_unconfirmed_regardless_of_activity_match():
    # T-ELIG-02: even a strong activity match must not read as "eligible"
    # while the classification requirement is still unknown.
    profile = CompanyProfile(activity_ids=(111,))
    result = score_fit(Tender(activity_id=111), profile, detail=None)
    assert result.classification_status == "unconfirmed"
    assert result.label == "غير مؤكد التصنيف"
    reasons = {r.factor: r for r in result.reasons}
    assert reasons["activity"].status == "matched"
    assert reasons["region"].status == "unknown"
    assert reasons["classification"].status == "unknown"
    # Activity's 50 points count; region/classification are excluded from
    # the ceiling entirely (not scored as failures).
    assert result.score == 50
    assert result.max_possible_score == 50


def test_activity_mismatch_scores_zero_for_that_factor():
    profile = CompanyProfile(activity_ids=(999,))
    result = score_fit(Tender(activity_id=111), profile, detail=None)
    reasons = {r.factor: r for r in result.reasons}
    assert reasons["activity"].status == "not_matched"
    assert reasons["activity"].points == 0


def test_classification_not_required_is_compliant_when_activity_and_region_match():
    profile = CompanyProfile(
        activity_ids=(111,),
        regions=("الرياض",),
    )
    detail = TenderDetailLike(
        classification_required=False,
        classification_text="غير مطلوب",
        execution_location="داخل المملكة منطقة الرياض الرياض",
    )
    result = score_fit(Tender(activity_id=111), profile, detail=detail)
    assert result.classification_status == "not_required"
    assert result.label == "متوافق مع المتطلبات المنشورة"
    assert result.score == 100
    assert result.max_possible_score == 100


def test_classification_required_and_matched():
    profile = CompanyProfile(
        activity_ids=(111,),
        regions=("الرياض",),
        classification_grades=("التكييف المركزي",),
    )
    detail = TenderDetailLike(
        classification_required=True,
        classification_text="أعمال الميكانيكية، نظام التكييف المركزي، المباني الخرسانية",
        execution_location="منطقة الرياض",
    )
    result = score_fit(Tender(activity_id=111), profile, detail=detail)
    assert result.classification_status == "matched"
    assert result.label == "متوافق مع المتطلبات المنشورة"
    assert result.score == 100


def test_classification_required_and_not_matched_never_defaults_favorably():
    profile = CompanyProfile(
        activity_ids=(111,),
        regions=("الرياض",),
        classification_grades=("أعمال الطرق",),
    )
    detail = TenderDetailLike(
        classification_required=True,
        classification_text="أعمال الميكانيكية، نظام التكييف المركزي",
        execution_location="منطقة الرياض",
    )
    result = score_fit(Tender(activity_id=111), profile, detail=detail)
    assert result.classification_status == "not_matched"
    assert result.label == "قد لا يستوفي المتطلبات المنشورة"
    reasons = {r.factor: r for r in result.reasons}
    assert reasons["classification"].points == 0
    assert result.score == 75  # activity + region only


def test_region_mismatch_flagged():
    profile = CompanyProfile(activity_ids=(111,), regions=("جدة",))
    detail = TenderDetailLike(
        classification_required=False,
        classification_text="غير مطلوب",
        execution_location="منطقة الرياض",
    )
    result = score_fit(Tender(activity_id=111), profile, detail=detail)
    reasons = {r.factor: r for r in result.reasons}
    assert reasons["region"].status == "not_matched"
    assert result.label == "قد لا يستوفي المتطلبات المنشورة"


def test_no_regions_declared_is_unknown_not_a_failure():
    profile = CompanyProfile(activity_ids=(111,))  # no regions declared
    detail = TenderDetailLike(
        classification_required=False,
        classification_text="غير مطلوب",
        execution_location="منطقة الرياض",
    )
    result = score_fit(Tender(activity_id=111), profile, detail=detail)
    reasons = {r.factor: r for r in result.reasons}
    assert reasons["region"].status == "unknown"
    assert reasons["region"].points == 0
    # region excluded from ceiling, not counted as a mismatch
    assert result.max_possible_score == 75
    assert result.label == "متوافق مع المتطلبات المنشورة"


def test_value_band_is_explicitly_not_built():
    result = score_fit(Tender(activity_id=111), CompanyProfile())
    assert "value_band" in result.not_built_factors
    assert not any(r.factor == "value_band" for r in result.reasons)
