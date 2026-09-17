"""Deterministic (non-AI) eligibility/fit scoring (PRD "Thaqip for
Contractors" §5.1, tests T-ELIG-01/T-ELIG-02).

Pure functions only: no I/O, no DB, no network. Callers pass in a tenant's
self-declared `CompanyProfile`, the tender's own row, and — when available —
the `TenderDetail` fetched from Etimad's detail-page components (see
thaqip_ingestion.etimad.details). The tender-details fetch is a separate,
already-existing concern; this module never fetches anything itself.

Honesty rules this module exists to enforce (not just document):

- If the classification requirement has not been fetched yet, the score is
  computed WITHOUT the classification factor (PRD §5.1: "fit score is
  computed without classification and shown as 'غير مؤكد التصنيف'") — it is
  never assumed compliant, never assumed non-compliant, and never silently
  scored as if the factor didn't exist (`classification_status` says exactly
  why). The same applies to the region factor, which is read from the same
  detail-page fetch.
- There is currently no verified per-tender estimated contract value in this
  codebase (`tenders.booklet_price` is the registration-document purchase
  fee, not the project value — using it as a value-band proxy would be a
  fabricated number). The "value band" factor from the PRD is therefore
  NOT BUILT: it is never scored, and `NOT_BUILT_FACTORS` says so explicitly
  rather than pretending three of four PRD-listed factors is "the" score.
- `label` uses the PRD's own Arabic wording (§5.1, §8) so the UI, this
  module, and the PRD stay in exact agreement. It is a plain "did the
  published requirements match", never a hidden claim of eligibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# PRD lists activity/region/value-band/classification. Value band has no
# honest data source yet (see module docstring) and is intentionally absent
# from WEIGHTS — it must not silently count as a 4th, always-missing factor
# that quietly caps every score.
NOT_BUILT_FACTORS = ("value_band",)

WEIGHTS = {
    "activity": 50,
    "region": 25,
    "classification": 25,
}
MAX_SCORE = sum(WEIGHTS.values())

LABEL_UNCONFIRMED = "غير مؤكد التصنيف"
LABEL_COMPLIANT = "متوافق مع المتطلبات المنشورة"
LABEL_MISMATCH = "قد لا يستوفي المتطلبات المنشورة"

# Substrings Etimad's own detail page uses when a classification field, or a
# stop-period/date field, is simply not applicable to this tender.
_NOT_REQUIRED_MARKERS = ("غير مطلوب", "لا يوجد")


@dataclass(frozen=True)
class CompanyProfile:
    activity_ids: tuple[int, ...] = ()
    regions: tuple[str, ...] = ()
    classification_grades: tuple[str, ...] = ()


class TenderLike(Protocol):
    activity_id: int | None


@dataclass(frozen=True)
class TenderDetailLike:
    """Duck-typed subset of a stored `tender_details` row this module reads.

    Deliberately narrower than the full DB row (or thaqip_ingestion.etimad.
    details.TenderDetail) so tests can build one without a database or a
    live fetch.
    """
    classification_required: bool | None = None
    classification_text: str | None = None
    execution_location: str | None = None


@dataclass(frozen=True)
class FitReason:
    factor: str  # 'activity' | 'region' | 'classification'
    status: str  # 'matched' | 'not_matched' | 'unknown' | 'not_applicable'
    weight: int
    points: int
    message_ar: str


@dataclass(frozen=True)
class FitScore:
    score: int  # 0..max_possible_score, both out of 100
    max_possible_score: int  # ceiling given which factors were actually evaluable
    label: str
    classification_status: str  # 'unconfirmed' | 'not_required' | 'matched' | 'not_matched'
    reasons: list[FitReason] = field(default_factory=list)
    not_built_factors: tuple[str, ...] = NOT_BUILT_FACTORS


def _classification_required(detail: TenderDetailLike | None) -> bool | None:
    if detail is None:
        return None
    if detail.classification_required is not None:
        return detail.classification_required
    if detail.classification_text is None:
        return None
    return not any(m in detail.classification_text for m in _NOT_REQUIRED_MARKERS)


def _score_activity(profile: CompanyProfile, tender: TenderLike) -> FitReason:
    weight = WEIGHTS["activity"]
    if not profile.activity_ids:
        return FitReason("activity", "unknown", weight, 0,
                          "لم يحدَّد نشاط الشركة في الملف؛ لا يمكن تقييم توافق النشاط.")
    if tender.activity_id is None:
        return FitReason("activity", "unknown", weight, 0,
                          "نشاط المنافسة غير مسجَّل لهذه المنافسة؛ لا يمكن تقييم توافق النشاط.")
    if tender.activity_id in profile.activity_ids:
        return FitReason("activity", "matched", weight, weight,
                          "نشاط المنافسة يطابق أحد أنشطة الشركة المسجَّلة.")
    return FitReason("activity", "not_matched", weight, 0,
                      "نشاط المنافسة لا يطابق أيًا من أنشطة الشركة المسجَّلة.")


def _score_region(profile: CompanyProfile, detail: TenderDetailLike | None) -> FitReason:
    weight = WEIGHTS["region"]
    if detail is None or detail.execution_location is None:
        return FitReason("region", "unknown", weight, 0,
                          "لم تُجلب بعد صفحة تفاصيل المنافسة؛ مكان التنفيذ غير مؤكد.")
    if not profile.regions:
        return FitReason("region", "unknown", weight, 0,
                          "لم تُحدَّد مناطق عمل الشركة في الملف؛ لا يمكن تقييم توافق الموقع.")
    if any(r in detail.execution_location for r in profile.regions):
        return FitReason("region", "matched", weight, weight,
                          f"مكان التنفيذ ({detail.execution_location}) يقع ضمن مناطق عمل الشركة.")
    return FitReason("region", "not_matched", weight, 0,
                      f"مكان التنفيذ ({detail.execution_location}) خارج مناطق عمل الشركة المسجَّلة.")


def _score_classification(profile: CompanyProfile, detail: TenderDetailLike | None) -> FitReason:
    weight = WEIGHTS["classification"]
    required = _classification_required(detail)
    if required is None:
        return FitReason("classification", "unknown", weight, 0,
                          "لم تُجلب بعد صفحة تفاصيل المنافسة؛ متطلب التصنيف غير مؤكد.")
    if not required:
        return FitReason("classification", "not_applicable", weight, weight,
                          "لا يوجد متطلب تصنيف منشور لهذه المنافسة.")
    text = detail.classification_text or ""
    if profile.classification_grades and any(g in text for g in profile.classification_grades):
        return FitReason("classification", "matched", weight, weight,
                          f"مجال التصنيف المطلوب ({text}) يطابق تصنيف الشركة المسجَّل.")
    return FitReason("classification", "not_matched", weight, 0,
                      f"مجال التصنيف المطلوب ({text}) لا يطابق أي تصنيف مسجَّل للشركة.")


def score_fit(
    tender: TenderLike,
    profile: CompanyProfile,
    detail: TenderDetailLike | None = None,
) -> FitScore:
    reasons = [
        _score_activity(profile, tender),
        _score_region(profile, detail),
        _score_classification(profile, detail),
    ]
    score = sum(r.points for r in reasons)
    max_possible = sum(r.weight for r in reasons if r.status != "unknown")

    classification_reason = reasons[2]
    if classification_reason.status == "unknown":
        classification_status = "unconfirmed"
    elif classification_reason.status == "not_applicable":
        classification_status = "not_required"
    elif classification_reason.status == "matched":
        classification_status = "matched"
    else:
        classification_status = "not_matched"

    # The PRD's own failure rule (§5.1): an unconfirmed classification
    # requirement must never be labelled as eligible, whatever the numeric
    # score from the other factors happens to be.
    if classification_status == "unconfirmed":
        label = LABEL_UNCONFIRMED
    elif any(r.status == "not_matched" for r in reasons):
        label = LABEL_MISMATCH
    elif max_possible == 0:
        # Nothing at all was evaluable (no profile, no details) — still not
        # a compliance claim.
        label = LABEL_UNCONFIRMED
    else:
        label = LABEL_COMPLIANT

    return FitScore(
        score=score,
        max_possible_score=max_possible,
        label=label,
        classification_status=classification_status,
        reasons=reasons,
    )
