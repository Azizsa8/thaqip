"""Shared Price-to-Win vocabulary: enums, value objects and the model output contract.

Pure python. No DB, no I/O, no network — every rule here is unit-testable and is
the single definition every other P2W module imports. The point of putting this
in one place is that the honesty rules of the product (suppress rather than
extrapolate; never dress a derived number as an observed one; always carry the
evidence that justifies a number) become type-level invariants instead of
conventions each caller has to remember.

The load-bearing rule is the suppression invariant on `PricePrediction`:
a prediction either carries an ordered p10/p50/p90 and no suppression reason, or
it carries a suppression reason and no numbers at all. There is no third state.
Constructing anything else raises ValueError.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

MODEL_VERSION = "p2w-0.1.0"

#: A prediction older than this contributes nothing to the freshness score.
FRESHNESS_HORIZON_DAYS = 730.0


class EvidenceTier(str, Enum):
    """How much the evidence behind a prediction can actually bear.

    The tier is not a quality badge — it decides what the system is *allowed to
    say*. Saudi public tenders are near-pure lowest-qualified-price auctions and
    per-vendor history in this dataset is sparse, so most subjects land in C/D
    and the correct product behaviour there is to show market facts and suppress
    the competitor price, never to extrapolate one from a couple of bids.
    """

    A = "A"  # strong competitor history + high similarity + fresh -> competitor prediction allowed
    B = "B"  # moderate history + good market comparables -> competitor range allowed but WIDENED
    C = "C"  # weak competitor history, strong market data -> competitor SUPPRESSED, market shown
    D = "D"  # weak market and competitor evidence -> no smart price, descriptive facts only

    @property
    def allows_competitor_prediction(self) -> bool:
        """True only where a competitor-level price may be shown at all."""
        return self in (EvidenceTier.A, EvidenceTier.B)

    @property
    def requires_widened_interval(self) -> bool:
        """Tier B may speak, but only with a deliberately widened interval."""
        return self is EvidenceTier.B

    @property
    def allows_market_prediction(self) -> bool:
        """Tier D has no usable market evidence either: facts only."""
        return self is not EvidenceTier.D


class PredictionScope(str, Enum):
    """What the prediction is about — these are read very differently by users."""

    MARKET = "MARKET"                  # where the market is likely to land
    COMPETITOR = "COMPETITOR"          # a named vendor's likely bid (highest bar)
    USER_OPTIMIZER = "USER_OPTIMIZER"  # the operator's own bid, from their own costs


class SuppressionReason(str, Enum):
    """Machine-readable reason a number was withheld.

    Every suppression must name one of these; "we just didn't show it" is not a
    permitted outcome, because the UI has to explain the absence to the user.
    """

    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    STALE_DATA = "STALE_DATA"
    LOW_SIMILARITY = "LOW_SIMILARITY"
    NO_COMPARABLE_TENDERS = "NO_COMPARABLE_TENDERS"
    TENANT_DATA_UNAVAILABLE = "TENANT_DATA_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    CALIBRATION_FAILED = "CALIBRATION_FAILED"


def freshness_score(median_age_days: float) -> int:
    """Map the median age of the supporting evidence to a 0-100 freshness score.

    Linear decay: 100 at 0 days, 0 at FRESHNESS_HORIZON_DAYS (2 years) and
    beyond. Negative ages (clock skew, a record dated in the future) clamp to
    100 rather than exceeding it. Deliberately simple and deterministic — this
    number is shown to users and must be explainable in one sentence.
    """
    if math.isnan(median_age_days):
        raise ValueError("median_age_days must be a real number")
    if median_age_days <= 0:
        return 100
    if median_age_days >= FRESHNESS_HORIZON_DAYS:
        return 0
    return round(100.0 * (1.0 - median_age_days / FRESHNESS_HORIZON_DAYS))


def _require_real_number(name: str, value: Any) -> float:
    """Coerce to float or raise ValueError naming the offending field.

    ValueError (not TypeError) throughout: to a caller of these contracts a bad
    value and a bad type are the same class of input problem, and one exception
    type keeps the validation surface small.
    """
    if value is None:
        raise ValueError(f"{name} must not be None")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric, got {type(value).__name__}") from exc
    if math.isnan(number):
        raise ValueError(f"{name} must not be NaN")
    return number


@dataclass(frozen=True)
class Quantiles:
    """A p10/p50/p90 triple. Ordering is the whole point, so it is checkable."""

    p10: float
    p50: float
    p90: float

    def validate(self) -> Quantiles:
        """Raise ValueError unless p10 <= p50 <= p90. Returns self for chaining."""
        for name, value in (("p10", self.p10), ("p50", self.p50), ("p90", self.p90)):
            _require_real_number(name, value)
        if not (self.p10 <= self.p50 <= self.p90):
            raise ValueError(
                f"quantiles must be ordered p10<=p50<=p90, got "
                f"p10={self.p10}, p50={self.p50}, p90={self.p90}"
            )
        return self

    @property
    def width(self) -> float:
        """Absolute p10..p90 spread — the honest measure of how vague this is."""
        return self.p90 - self.p10

    def to_dict(self) -> dict[str, float]:
        return {"p10": self.p10, "p50": self.p50, "p90": self.p90}


DIRECTIONS = ("increases", "decreases", "neutral")
FACTOR_KINDS = ("observed", "derived", "predicted")


@dataclass
class ExplanationFactor:
    """One reason behind a number, tagged with what kind of claim it is.

    `kind` maps directly onto the house display rule (مُلاحظ / متوقع / إدخالك):
    an observed fact and a model inference must never be rendered alike, so the
    distinction is carried in the data rather than decided in the template.
    """

    name: str
    direction: str
    weight: float
    kind: str
    detail: str = ""
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ExplanationFactor.name must be non-empty")
        if self.direction not in DIRECTIONS:
            raise ValueError(
                f"direction must be one of {DIRECTIONS}, got {self.direction!r}"
            )
        if self.kind not in FACTOR_KINDS:
            raise ValueError(f"kind must be one of {FACTOR_KINDS}, got {self.kind!r}")
        self.weight = _require_real_number("weight", self.weight)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction,
            "weight": self.weight,
            "kind": self.kind,
            "detail": self.detail,
            "evidence_ref": self.evidence_ref,
        }


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class PricePrediction:
    """The model output contract shared by every P2W producer and consumer.

    Invariant, enforced at construction:
      * suppression_reason set   -> p10, p50, p90 and win_probability are None.
      * suppression_reason unset -> p10, p50, p90 are present and ordered.
    A caller that has no number to give must say why, and a caller that gives a
    number may not simultaneously claim it withheld one.
    """

    tender_id: int
    prediction_scope: PredictionScope
    subject_id: int | None = None
    p10: float | None = None
    p50: float | None = None
    p90: float | None = None
    expected_value: float | None = None
    win_probability: float | None = None
    confidence_score: int | None = None
    similarity_confidence: int | None = None
    data_freshness_score: int | None = None
    evidence_count: int = 0
    evidence_tier: EvidenceTier | None = None
    model_version: str = MODEL_VERSION
    feature_snapshot_id: str | None = None
    seed: int | None = None
    generated_at: datetime = field(default_factory=_utcnow)
    explanation_factors: list[ExplanationFactor] = field(default_factory=list)
    suppression_reason: SuppressionReason | None = None

    def __post_init__(self) -> None:
        # Accept plain strings at the boundary (JSON, DB rows) but normalise to
        # enums so downstream code can rely on the type.
        if isinstance(self.prediction_scope, str):
            self.prediction_scope = PredictionScope(self.prediction_scope)
        if isinstance(self.evidence_tier, str):
            self.evidence_tier = EvidenceTier(self.evidence_tier)
        if isinstance(self.suppression_reason, str):
            self.suppression_reason = SuppressionReason(self.suppression_reason)

        if self.suppression_reason is not None:
            populated = [
                name for name, value in (
                    ("p10", self.p10), ("p50", self.p50), ("p90", self.p90),
                    ("win_probability", self.win_probability),
                ) if value is not None
            ]
            if populated:
                raise ValueError(
                    "suppressed prediction must not carry numeric values; got "
                    + ", ".join(populated)
                )
        else:
            missing = [
                name for name, value in (
                    ("p10", self.p10), ("p50", self.p50), ("p90", self.p90),
                ) if value is None
            ]
            if missing:
                raise ValueError(
                    "unsuppressed prediction requires " + ", ".join(missing)
                    + "; set a suppression_reason instead of omitting them"
                )
            Quantiles(float(self.p10), float(self.p50), float(self.p90)).validate()

        if self.win_probability is not None and not 0.0 <= self.win_probability <= 1.0:
            raise ValueError(
                f"win_probability must be in [0,1], got {self.win_probability}"
            )
        for name in ("confidence_score", "similarity_confidence", "data_freshness_score"):
            value = getattr(self, name)
            if value is not None and not 0 <= value <= 100:
                raise ValueError(f"{name} must be in [0,100], got {value}")
        if self.evidence_count < 0:
            raise ValueError("evidence_count must not be negative")
        if not self.model_version:
            raise ValueError("model_version is required")

    @property
    def is_suppressed(self) -> bool:
        return self.suppression_reason is not None

    @property
    def quantiles(self) -> Quantiles | None:
        """The quantile triple, or None when this prediction is suppressed."""
        if self.is_suppressed:
            return None
        return Quantiles(float(self.p10), float(self.p50), float(self.p90))

    @classmethod
    def suppressed(
        cls,
        tender_id: int,
        scope: PredictionScope | str,
        reason: SuppressionReason | str,
        *,
        subject_id: int | None = None,
        evidence_count: int = 0,
        evidence_tier: EvidenceTier | str | None = None,
        confidence_score: int | None = None,
        similarity_confidence: int | None = None,
        data_freshness_score: int | None = None,
        model_version: str = MODEL_VERSION,
        feature_snapshot_id: str | None = None,
        seed: int | None = None,
        explanation_factors: list[ExplanationFactor] | None = None,
        generated_at: datetime | None = None,
    ) -> PricePrediction:
        """Build a prediction that withholds every number and says why.

        Note what is still carried: evidence_count, tier and the explanation
        factors. A suppression the user cannot interrogate is just a blank space,
        so the reasons for the silence travel with it.
        """
        return cls(
            tender_id=tender_id,
            prediction_scope=scope,
            subject_id=subject_id,
            p10=None, p50=None, p90=None,
            expected_value=None,
            win_probability=None,
            confidence_score=confidence_score,
            similarity_confidence=similarity_confidence,
            data_freshness_score=data_freshness_score,
            evidence_count=evidence_count,
            evidence_tier=evidence_tier,
            model_version=model_version,
            feature_snapshot_id=feature_snapshot_id,
            seed=seed,
            generated_at=generated_at if generated_at is not None else _utcnow(),
            explanation_factors=list(explanation_factors or []),
            suppression_reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation. Enums become their string values, the
        timestamp becomes ISO-8601, factors become plain dicts."""
        return {
            "tender_id": self.tender_id,
            "prediction_scope": self.prediction_scope.value,
            "subject_id": self.subject_id,
            "p10": self.p10,
            "p50": self.p50,
            "p90": self.p90,
            "expected_value": self.expected_value,
            "win_probability": self.win_probability,
            "confidence_score": self.confidence_score,
            "similarity_confidence": self.similarity_confidence,
            "data_freshness_score": self.data_freshness_score,
            "evidence_count": self.evidence_count,
            "evidence_tier": self.evidence_tier.value if self.evidence_tier else None,
            "model_version": self.model_version,
            "feature_snapshot_id": self.feature_snapshot_id,
            "seed": self.seed,
            "generated_at": self.generated_at.isoformat(),
            "explanation_factors": [f.to_dict() for f in self.explanation_factors],
            "suppression_reason": (
                self.suppression_reason.value if self.suppression_reason else None
            ),
            "is_suppressed": self.is_suppressed,
        }
