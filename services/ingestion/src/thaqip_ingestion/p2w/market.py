"""Market quantile model (PRD FR-002): where comparable tenders actually landed.

This is the L1 capability of Price-to-Win. It answers one question — *given the
awarded values of tenders comparable to this one, and adjusting for the fact
that older money is not today's money, what is the P10/P50/P90 of the market?*
— and it is the number that must still be produced when every competitor-level
model is (correctly) suppressed.

How it works
------------
1. Retrieval: ``p2w.similarity.find_similar_tenders`` supplies scored, already
   point-in-time-gated comparables (nothing knowable only after ``as_of`` can
   enter the sample). The caller may pass a pre-computed list instead.
2. Weighting: each comparable contributes ``similarity_score * recency_weight``
   with ``recency_weight = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)``. A
   two-year-old award therefore counts a quarter of a same-week one.
3. Time adjustment: each historical award is restated in ``as_of`` money with
   GASTAT's national CPI (``p2w.indices``): level at as_of / level at the award
   date, using only index months already published by as_of. A comparable the
   index cannot cover falls back to ``(1 + DEFAULT_ANNUAL_INFLATION) **
   age_years``. How many comparables used each method is recorded in the
   ``time_adjustment`` explanation factor.
4. Quantiles: ``weighted_quantile`` over the adjusted values.
5. Gating: ``p2w.evidence.market_evidence`` grades the evidence. If the tier
   forbids a market statement, a suppressed ``PricePrediction`` is returned with
   the machine-readable reason and no numbers at all.

Honest limitations
------------------
* CPI is an economy-wide price level, not a sector cost index. GASTAT's
  construction cost index starts only in June 2025 and its wholesale index has
  an apparent base-year break in January 2018, so neither is used yet. The
  flat 2%/yr remains only as the fallback for dates outside the CPI series.
* The sample is awarded *contract values*, not bids. It describes where winning
  prices landed, which — given that ~96% of multi-bidder awards in this corpus
  went to the lowest technically-compliant offer — is close to the market's
  lower envelope, not its centre of gravity of submitted bids.
* Comparability is a heuristic similarity score, not an engineering scope
  match. Two tenders scoring 0.8 may still be different work.
* With this corpus most tenders have few awarded comparables, so suppression is
  a frequent and *correct* output. An empty or thin sample is never padded.
"""
from __future__ import annotations

import bisect
import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contracts import (
    MODEL_VERSION,
    EvidenceTier,
    ExplanationFactor,
    PredictionScope,
    PricePrediction,
    Quantiles,
    SuppressionReason,
)

# --------------------------------------------------------------------------
# tunable constants — every magic number in this module lives here
# --------------------------------------------------------------------------

#: Annual index used to bring historical awards to as_of-equivalent money.
#: ASSUMPTION, NOT A MEASUREMENT: a flat 2%/yr stand-in for a real published
#: cost index. Chosen because it is close to the long-run Saudi CPI and because
#: a conservative small number keeps the adjustment from dominating the answer.
#: Replace with a sector cost index (and make it per-year, not flat) before
#: anyone prices a large bid off this.
DEFAULT_ANNUAL_INFLATION = 0.02

#: Days per year used for both the inflation exponent and age reporting.
#: 365.25 (not 365) so that multi-year adjustments do not drift by a day a year.
DAYS_PER_YEAR = 365.25

#: Recency half-life for sample weights: a comparable one year old counts half
#: as much as a same-day one. One year matches the annual budget/tender cycle of
#: Saudi public buyers, which is the cadence at which prices actually reset.
RECENCY_HALF_LIFE_DAYS = 365.0

#: Comparables scoring below this contribute nothing. The similarity score is a
#: weighted average of six components each in [0,1]; below 0.20 the match is
#: carried by neutral defaults rather than by anything actually matching, and
#: including such rows would launder noise into the quantiles.
MIN_CONTRIBUTING_SIMILARITY = 0.20

#: Awards at or below this value are dropped as data errors, not signals: a
#: public tender award of a few riyals is a placeholder or a unit-price row.
MIN_PLAUSIBLE_AWARD_VALUE = 1.0

#: Tier B is allowed to speak but must speak vaguely: the p10..p50 and p50..p90
#: half-widths are multiplied by this before publication.
TIER_B_WIDENING_FACTOR = 1.5

#: How many individual comparable tenders are cited by name in the explanation.
#: Three keeps the UI readable while still letting a user audit the answer.
TOP_CONTRIBUTORS_CITED = 3

#: Contract floor/ceiling on the number of explanation factors returned.
MIN_EXPLANATION_FACTORS = 3
MAX_EXPLANATION_FACTORS = 6

#: Quantiles this model publishes.
Q_LOW, Q_MID, Q_HIGH = 0.10, 0.50, 0.90

#: Identifier prefix for evidence references pointing at a tender row.
EVIDENCE_REF_TENDER = "tender"


# --------------------------------------------------------------------------
# sample construction (pure — no DB, no clock)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketSampleItem:
    """One comparable award as it enters the quantile computation.

    ``observed_value`` is the fact on record; ``adjusted_value`` is that fact
    restated in as_of-equivalent money. They are kept apart deliberately: the UI
    must be able to show the observed number as observed.
    """

    tender_id: int
    name: str
    observed_value: float
    adjusted_value: float
    age_days: float
    similarity: float
    recency_weight: float
    weight: float
    #: "index:<series>" when a published index restated the value, else "flat".
    adjustment: str = "flat"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tender_id": self.tender_id,
            "name": self.name,
            "observed_value": self.observed_value,
            "adjusted_value": self.adjusted_value,
            "age_days": self.age_days,
            "similarity": self.similarity,
            "recency_weight": self.recency_weight,
            "weight": self.weight,
            "adjustment": self.adjustment,
        }


def recency_weight(age_days: float, half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> float:
    """``0.5 ** (age_days / half_life)`` — 1.0 today, 0.5 after one half-life.

    Ages at or below zero clamp to 1.0 rather than exceeding it: a record dated
    after ``as_of`` is a clock artefact, not extra-fresh evidence.
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    if math.isnan(age_days):
        raise ValueError("age_days must be a real number")
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def inflation_factor(
    age_days: float, annual_rate: float = DEFAULT_ANNUAL_INFLATION
) -> float:
    """Multiplier bringing money that is ``age_days`` old to as_of-equivalent.

    ``(1 + annual_rate) ** (age_days / DAYS_PER_YEAR)``. Ages at or below zero
    return 1.0 — we never deflate a future-dated record.
    """
    if math.isnan(age_days):
        raise ValueError("age_days must be a real number")
    if annual_rate <= -1.0:
        raise ValueError("annual_rate must be greater than -1.0")
    if age_days <= 0:
        return 1.0
    return (1.0 + annual_rate) ** (age_days / DAYS_PER_YEAR)


def _field(item: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a SimilarTender-like object or a plain mapping."""
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def build_sample(
    similar: Sequence[Any],
    *,
    annual_rate: float = DEFAULT_ANNUAL_INFLATION,
    min_similarity: float = MIN_CONTRIBUTING_SIMILARITY,
    index: Any = None,
    as_of: datetime | None = None,
) -> list[MarketSampleItem]:
    """Turn scored comparables into weighted, time-adjusted sample points.

    Accepts ``similarity.SimilarTender`` objects or mappings with the same field
    names. Rows are dropped — never repaired — when they are excluded by
    retrieval, carry no usable award value, have an unusable age, or score below
    ``min_similarity``. The result is sorted by tender id so the sample (and
    therefore the snapshot hash) is deterministic.
    """
    sample: list[MarketSampleItem] = []
    for item in similar:
        if _field(item, "exclusion_reason") is not None:
            continue
        raw_value = _field(item, "award_value")
        if raw_value is None:
            continue
        value = float(raw_value)
        if not math.isfinite(value) or value < MIN_PLAUSIBLE_AWARD_VALUE:
            continue
        score = float(_field(item, "total_score", 0.0) or 0.0)
        if not math.isfinite(score) or score < min_similarity:
            continue
        age = _field(item, "age_days")
        if age is None:
            continue
        age = float(age)
        if not math.isfinite(age):
            continue
        rec = recency_weight(age)
        weight = score * rec
        if weight <= 0.0:
            continue
        restated = index.factor(age, as_of) if (index is not None and as_of is not None) else None
        if restated is not None:
            factor, adjustment = restated[0], f"index:{index.name}"
        else:
            factor, adjustment = inflation_factor(age, annual_rate), "flat"
        sample.append(
            MarketSampleItem(
                tender_id=int(_field(item, "tender_id")),
                name=str(_field(item, "name") or ""),
                observed_value=value,
                adjusted_value=value * factor,
                age_days=age,
                similarity=score,
                recency_weight=rec,
                weight=weight,
                adjustment=adjustment,
            )
        )
    sample.sort(key=lambda s: s.tender_id)
    return sample


# --------------------------------------------------------------------------
# the numeric heart: weighted quantiles in pure python
# --------------------------------------------------------------------------


def weighted_quantile(
    values: Sequence[float], weights: Sequence[float], q: float
) -> float:
    """Weighted quantile with linear interpolation. Pure python (no numpy).

    Method: sort the points by value, take cumulative weights ``C_i``, and place
    point *i* at the normalized position

        ``p_i = (C_i - w_i/2 - w_1/2) / (S - (w_1 + w_n)/2)``

    which runs from 0 at the smallest point to 1 at the largest, then linearly
    interpolate between the two points bracketing ``q``.

    The half-weight terms make this reduce **exactly** to the ordinary
    linear-interpolation quantile (numpy's default) when all weights are equal:
    with ``w_i = w`` the expression collapses to ``(i - 1) / (n - 1)``. It is
    also symmetric — reversing the sample mirrors the result — which the plain
    ``(C_i - w_i/2)/S`` variant is not with respect to the plain quantile.

    The result never falls outside ``[min(values), max(values)]``: this model
    interpolates between observed awards, it does not extrapolate past them.

    Raises ValueError on empty input, mismatched lengths, non-finite numbers,
    negative weights, ``q`` outside [0, 1], or an all-zero weight vector.
    """
    if len(values) != len(weights):
        raise ValueError(
            f"values and weights must have equal length, got {len(values)} and {len(weights)}"
        )
    if not values:
        raise ValueError("weighted_quantile needs at least one point")
    if math.isnan(q) or not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0,1], got {q}")

    points: list[tuple[float, float]] = []
    for value, weight in zip(values, weights):
        v, w = float(value), float(weight)
        if not math.isfinite(v):
            raise ValueError(f"values must be finite, got {value!r}")
        if not math.isfinite(w):
            raise ValueError(f"weights must be finite, got {weight!r}")
        if w < 0.0:
            raise ValueError(f"weights must not be negative, got {weight!r}")
        if w > 0.0:  # zero-weight points contribute nothing and are dropped
            points.append((v, w))
    if not points:
        raise ValueError("weighted_quantile needs at least one strictly positive weight")

    points.sort(key=lambda p: p[0])
    if len(points) == 1:
        return points[0][0]

    sorted_values = [p[0] for p in points]
    sorted_weights = [p[1] for p in points]
    total = math.fsum(sorted_weights)
    denominator = total - (sorted_weights[0] + sorted_weights[-1]) / 2.0
    if denominator <= 0.0:  # pragma: no cover - unreachable with positive weights
        return sorted_values[len(sorted_values) // 2]

    positions: list[float] = []
    cumulative = 0.0
    offset = sorted_weights[0] / 2.0
    for value_weight in sorted_weights:
        cumulative += value_weight
        positions.append((cumulative - value_weight / 2.0 - offset) / denominator)
    # Pin the ends: floating-point error must not push p_1 below 0 or p_n above 1.
    positions[0] = 0.0
    positions[-1] = 1.0

    if q <= 0.0:
        return sorted_values[0]
    if q >= 1.0:
        return sorted_values[-1]

    index = bisect.bisect_left(positions, q)
    if positions[index] == q:
        return sorted_values[index]
    lo, hi = index - 1, index
    span = positions[hi] - positions[lo]
    if span <= 0.0:  # identical positions (equal values); either endpoint will do
        return sorted_values[hi]
    fraction = (q - positions[lo]) / span
    return sorted_values[lo] + fraction * (sorted_values[hi] - sorted_values[lo])


def fraction_at_or_below(values: Sequence[float], price: float) -> float:
    """Empirical fraction of ``values`` that are <= ``price``. Observed fact.

    Unweighted on purpose: this is a count of what happened, not a model.
    """
    if not values:
        raise ValueError("fraction_at_or_below needs at least one value")
    hits = sum(1 for v in values if float(v) <= float(price))
    return hits / len(values)


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


def feature_snapshot_id(
    tender_ids: Sequence[int],
    as_of: datetime,
    *,
    model_version: str = MODEL_VERSION,
) -> str:
    """Stable 16-hex-char id of (contributing tender ids, as_of date, version).

    Deterministic by construction — sha256 over a canonical string, never a
    random or time-seeded value — so the same inputs reproduce the same id in
    another process, and any change of sample, day or model version changes it.
    The as_of *date* (not instant) is used: re-running the same day must not
    invent a new snapshot.
    """
    ids = ",".join(str(int(i)) for i in sorted(set(tender_ids)))
    payload = f"{model_version}|{as_of.date().isoformat()}|{ids}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# explanation
# --------------------------------------------------------------------------


def _time_adjustment_factor(
    sample: Sequence[MarketSampleItem], *, annual_rate: float, median_factor: float
) -> ExplanationFactor:
    indexed = [s for s in sample if s.adjustment.startswith("index:")]
    flat = len(sample) - len(indexed)
    direction = ("increases" if median_factor > 1.0
                 else "decreases" if median_factor < 1.0 else "neutral")
    if not indexed:
        detail = (f"تعديل زمني بافتراض مؤشر سنوي {annual_rate:.1%}؛ "
                  f"معامل الوسيط {median_factor:.4f} (افتراض، وليس مؤشر تكلفة حقيقي)")
    else:
        series = indexed[0].adjustment.split(":", 1)[1]
        label = "مؤشر أسعار المستهلك الوطني" if series == "cpi.general" else series
        detail = (f"تعديل زمني بـ{label} (الهيئة العامة للإحصاء) لـ{len(indexed)} عقدًا؛ "
                  f"معامل الوسيط {median_factor:.4f}")
        if flat:
            detail += f"؛ {flat} خارج نطاق المؤشر عُدّلت بافتراض {annual_rate:.1%} سنويًا"
    return ExplanationFactor(
        name="time_adjustment", direction=direction, weight=round(median_factor - 1.0, 6),
        kind="derived", detail=detail)


def _explanation_factors(
    sample: Sequence[MarketSampleItem],
    *,
    tier: EvidenceTier,
    annual_rate: float,
    median_factor: float,
    widened: bool,
) -> list[ExplanationFactor]:
    """3-6 factors: sample size, the time assumption, recency, top contributors."""
    factors: list[ExplanationFactor] = [
        ExplanationFactor(
            name="sample_size",
            direction="neutral",
            weight=float(len(sample)),
            kind="observed",
            detail=(
                f"{len(sample)} عقد مُرسى مشابه دخل الحساب "
                f"(كل الأرقام مُلاحظة من عقود سابقة)"
            ),
        ),
        _time_adjustment_factor(sample, annual_rate=annual_rate, median_factor=median_factor),
        ExplanationFactor(
            name="recency_weighting",
            direction="neutral",
            weight=round(
                sum(s.recency_weight for s in sample) / len(sample) if sample else 0.0, 6
            ),
            kind="derived",
            detail=(
                f"ترجيح بنصف عمر {RECENCY_HALF_LIFE_DAYS:.0f} يوم؛ "
                f"متوسط وزن الحداثة للعينة"
            ),
        ),
    ]

    top = sorted(sample, key=lambda s: (-s.weight, s.tender_id))[:TOP_CONTRIBUTORS_CITED]
    remaining = MAX_EXPLANATION_FACTORS - len(factors) - (1 if widened else 0)
    for item in top[: max(0, remaining)]:
        factors.append(
            ExplanationFactor(
                name=f"comparable_tender_{item.tender_id}",
                direction="neutral",
                weight=round(item.weight, 6),
                kind="observed",
                detail=(
                    f"{item.name[:60]} — قيمة الإرساء {item.observed_value:,.0f} ر.س "
                    f"قبل {item.age_days:.0f} يوم (تشابه {item.similarity:.2f})"
                ),
                evidence_ref=f"{EVIDENCE_REF_TENDER}:{item.tender_id}",
            )
        )
    if widened:
        factors.append(
            ExplanationFactor(
                name="tier_b_widening",
                direction="neutral",
                weight=TIER_B_WIDENING_FACTOR,
                kind="derived",
                detail=(
                    f"المستوى {tier.value}: تم توسيع النطاق بمعامل "
                    f"{TIER_B_WIDENING_FACTOR} لضعف الأدلة"
                ),
            )
        )
    return factors[:MAX_EXPLANATION_FACTORS]


# --------------------------------------------------------------------------
# DB-touching entry points
# --------------------------------------------------------------------------


def _resolve_cutoff(tender: Mapping[str, Any], as_of: datetime | None) -> datetime:
    """The single point-in-time cutoff used by retrieval *and* by grading.

    An explicit ``as_of`` wins outright. Otherwise the two sibling modules each
    derive one (``evidence.resolve_as_of`` stops at last_offer_date and then
    falls back to now; ``similarity.point_in_time`` also accepts published_at)
    and this takes the **earlier** of the two. Resolving it once here means the
    evidence grade and the sample can never be computed against two different
    horizons, and taking the earlier one means an ambiguous tender is gated more
    strictly rather than less — a leak is the worst failure this model can have.
    """
    from . import evidence as evidence_mod
    from . import similarity as similarity_mod

    if as_of is not None:
        return evidence_mod.resolve_as_of(dict(tender), as_of)
    derived = evidence_mod.resolve_as_of(dict(tender), None)
    published = similarity_mod.point_in_time(tender)
    return min(derived, published) if published is not None else derived


async def _comparables(
    conn: Any,
    *,
    tender: Mapping[str, Any],
    as_of: datetime | None,
    similar: Sequence[Any] | None,
) -> Sequence[Any]:
    """Retrieval, unless the caller already did it. Local import so a test can
    monkeypatch ``p2w.similarity.find_similar_tenders`` without a database."""
    if similar is not None:
        return similar
    from . import similarity  # local import: keeps the seam patchable

    return await similarity.find_similar_tenders(conn, tender=tender, as_of=as_of)


async def market_quantiles(
    conn: Any,
    *,
    tender: Mapping[str, Any],
    as_of: datetime | None = None,
    similar: Sequence[Any] | None = None,
    annual_rate: float = DEFAULT_ANNUAL_INFLATION,
    index: Any = None,
) -> PricePrediction:
    """Inflation- and recency-adjusted P10/P50/P90 of comparable awarded values.

    Returns a fully populated ``PricePrediction`` with ``scope=MARKET``, or a
    suppressed one carrying a ``SuppressionReason`` when the evidence tier does
    not license a market statement or the usable sample is empty. There is no
    third outcome: this function never returns a number it cannot support.
    """
    from . import evidence as evidence_mod  # local import: patchable seam

    cutoff = _resolve_cutoff(tender, as_of)
    tender_id = int(tender["id"])

    market_ev = await evidence_mod.market_evidence(conn, tender=tender, as_of=cutoff)

    def _suppress(
        reason: SuppressionReason, *, factors: list[ExplanationFactor] | None = None
    ) -> PricePrediction:
        return PricePrediction.suppressed(
            tender_id=tender_id,
            scope=PredictionScope.MARKET,
            reason=reason,
            evidence_count=market_ev.comparable_count,
            evidence_tier=market_ev.tier,
            similarity_confidence=market_ev.similarity_confidence,
            data_freshness_score=market_ev.freshness,
            explanation_factors=factors or [],
        )

    if not market_ev.tier.allows_market_prediction:
        return _suppress(market_ev.suppression or SuppressionReason.INSUFFICIENT_EVIDENCE)

    comparables = await _comparables(conn, tender=tender, as_of=cutoff, similar=similar)
    if index is None:
        from . import indices as indices_mod  # local import: patchable seam

        index = await indices_mod.load_series(conn)
    sample = build_sample(comparables, annual_rate=annual_rate, index=index, as_of=cutoff)
    if not sample:
        # The evidence module counted awarded comparables in the same activity;
        # retrieval scores them and may find none of them actually comparable.
        return _suppress(SuppressionReason.NO_COMPARABLE_TENDERS)

    values = [s.adjusted_value for s in sample]
    weights = [s.weight for s in sample]
    p10 = weighted_quantile(values, weights, Q_LOW)
    p50 = weighted_quantile(values, weights, Q_MID)
    p90 = weighted_quantile(values, weights, Q_HIGH)

    widened = market_ev.tier.requires_widened_interval
    if widened:
        p10 = max(0.0, p50 - TIER_B_WIDENING_FACTOR * (p50 - p10))
        p90 = p50 + TIER_B_WIDENING_FACTOR * (p90 - p50)
    quantiles = Quantiles(p10=p10, p50=p50, p90=p90).validate()

    width_ratio = (quantiles.width / p50) if p50 > 0 else 0.0
    confidence = evidence_mod.confidence_score(
        tier=market_ev.tier,
        # The recency-weighted mass, not the raw row count: five two-year-old
        # awards are not five fresh ones.
        evidence_count=sum(s.recency_weight for s in sample),
        freshness=market_ev.freshness,
        similarity_confidence=market_ev.similarity_confidence,
        interval_width_ratio=width_ratio,
    )
    median_factor = sorted(s.adjusted_value / s.observed_value for s in sample)[
        len(sample) // 2
    ]

    return PricePrediction(
        tender_id=tender_id,
        prediction_scope=PredictionScope.MARKET,
        subject_id=None,
        p10=quantiles.p10,
        p50=quantiles.p50,
        p90=quantiles.p90,
        expected_value=quantiles.p50,
        confidence_score=confidence,
        similarity_confidence=market_ev.similarity_confidence,
        data_freshness_score=market_ev.freshness,
        evidence_count=len(sample),
        evidence_tier=market_ev.tier,
        model_version=MODEL_VERSION,
        feature_snapshot_id=feature_snapshot_id(
            [s.tender_id for s in sample], cutoff
        ),
        seed=None,  # closed-form: no Monte Carlo, nothing random to reproduce
        explanation_factors=_explanation_factors(
            sample,
            tier=market_ev.tier,
            annual_rate=annual_rate,
            median_factor=median_factor,
            widened=widened,
        ),
    )


async def market_curve(
    conn: Any,
    *,
    tender: Mapping[str, Any],
    as_of: datetime | None = None,
    grid: Sequence[float],
    similar: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """Observed price-percentile curve over ``grid``.

    For each candidate price, the fraction of comparable *winning awards* at or
    below it. Every entry is ``kind='observed'``: this is a count of recorded
    outcomes, with no model and no weighting, and it uses the observed award
    values rather than the inflation-adjusted ones so that nothing derived
    leaks into a curve labelled observed.

    Returns ``[]`` when there is no usable comparable set — an empty curve is
    the honest answer, and the caller must render the absence rather than a
    flat line at zero.
    """
    comparables = await _comparables(
        conn, tender=tender, as_of=_resolve_cutoff(tender, as_of), similar=similar
    )
    sample = build_sample(comparables)
    if not sample:
        return []
    observed = [s.observed_value for s in sample]
    return [
        {
            "price": float(price),
            "fraction_at_or_below": fraction_at_or_below(observed, price),
            "sample_size": len(observed),
            "kind": "observed",
            "model_version": MODEL_VERSION,
        }
        for price in grid
    ]
