"""Competitor quantile model (PRD FR-005) and undercut risk (FR-006).

What is modelled, and why in ratio space
----------------------------------------
Saudi public tenders differ by orders of magnitude in size, so a vendor's raw
bid history says almost nothing on its own. What *does* transfer between tenders
is how a vendor prices **relative to the price that won**:

    r = offer_value / award_value      (of the tender the offer was made on)
    y = ln(r)

``award_value`` is the winning price, and across this corpus the lowest
technically compliant offer won ~96% of the time, so ``r`` is essentially
"how far above the winner did this vendor bid". ``r >= 1`` by construction for
every observation in the current data, hence ``y >= 0``. Working in logs makes
the target additive, keeps the back-transform positive, and makes a lognormal
the natural fitted family.

For a *new* tender we do not know its winning price, so the market model's P50
(``p2w.market.market_quantiles``, scope MARKET) stands in as the baseline, and a
competitor quantile in SAR is

    price_q = market_p50 * exp(mu_post + z_q * sigma_post)

Hierarchical partial pooling
----------------------------
Per-vendor history is *thin* (the busiest vendor in the corpus has 25 observed
offers; the median vendor has one or two). Fitting a vendor-specific lognormal
on two points and reporting its quantiles would be fabricated precision. So the
vendor fit is shrunk toward the vendor's activity-category prior, James-Stein /
empirical-Bayes style:

    w        = n_eff / (n_eff + SHRINKAGE_K)
    mu_post  = w * mu_vendor + (1 - w) * mu_prior
    var_post = w * var_vendor + (1 - w) * var_prior
               + w * (1 - w) * (mu_vendor - mu_prior)^2     <- disagreement term

The disagreement term is the variance of the two-component mixture: when a
sparse vendor's mean disagrees with its category, the posterior gets *wider*
rather than confidently splitting the difference. Below
``SIGMA_FLOOR_POOLING_WEIGHT`` the posterior sigma is additionally floored at
the prior sigma, so a vendor with a handful of coincidentally similar bids can
never look tighter than the category it was pooled into.

The hard rule
-------------
Partial pooling makes a sparse vendor's number *safe*, not *earned*. Below the
Tier-B evidence threshold there is no competitor number at all: ``evidence.
competitor_evidence`` is consulted first and Tier C or D returns a suppressed
``PricePrediction``. There is no best-effort fallback for competitor-specific
pricing — the fallback is the market range, produced by a different module.
Tier B computes, but widens the interval by ``TIER_B_WIDENING_FACTOR`` and
docks the confidence score.

Honest limitations
------------------
* On the present corpus this function suppresses for almost every vendor. That
  is the correct output. Only a handful of vendors clear Tier B at all.
* The baseline is the market model's P50, which is itself a prediction. Its
  error compounds with the ratio model's, and the returned interval reflects
  only the *ratio* uncertainty — not the uncertainty in the baseline. A
  competitor band is therefore narrower than the true predictive band.
* ``r`` is defined against the realised winning price, so it is a
  *relative-to-winner* quantity, not an absolute pricing style. A vendor that
  only ever bids in easy, uncontested categories will look disciplined here.
* Point-in-time safety comes from ``COALESCE(awards.awarded_at, awards.
  created_at) < as_of``. ``awarded_at`` is NULL throughout this corpus, so the
  ingestion timestamp stands in; it is never earlier than the true award date,
  so the gate can only drop rows we were entitled to use, never leak a future
  fact. It also means recency decay is currently near-inert (everything was
  ingested in the same week) and ``n_eff`` sits close to the raw count.
* Cross-activity observations are down-weighted, not excluded — with samples
  this small, discarding them would leave nothing. The weight is a judgement
  call (``CROSS_ACTIVITY_WEIGHT``), not an estimated quantity.
* ``undercut_risk`` reads a lognormal back off the emitted p10/p50/p90. For a
  Tier-B (widened) prediction that lognormal is deliberately over-dispersed, so
  the risk it reports is pulled toward 0.5 — under-confident by design.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

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
# Module constants. Every magic number lives here with its justification.
# --------------------------------------------------------------------------

#: Pooling strength. w = n / (n + K), so K is the observation count at which a
#: vendor's own history and its category prior carry equal weight. 5 is chosen
#: to match the shape of this corpus: the busiest vendor has 25 offers and the
#: typical one has 1-3, so K=5 leaves the typical vendor firmly pooled (n=2 ->
#: w=0.29) while letting a genuinely well-observed vendor speak (n=25 -> w=0.83).
SHRINKAGE_K = 5.0

#: Recency decay half-life for observation weights, in days. Matches
#: ``evidence.HALF_LIFE_DAYS`` so the weight a row carries here and the
#: ``effective_n`` the tier ladder reads are on the same scale.
RECENCY_HALF_LIFE_DAYS = 365.0

#: Weight multiplier for an observation made in a different activity category.
#: Not zero, because excluding them would empty most vendors' samples; not one,
#: because pricing behaviour is category-dependent. Half is the honest midpoint
#: and is a judgement, not an estimate.
CROSS_ACTIVITY_WEIGHT = 0.5

#: Tier B may state a competitor range only with a deliberately widened one.
#: Log-space deviations from the median are multiplied by this. 1.4 (>= the 1.3
#: floor mandated by the spec) roughly restores the coverage a half-strength
#: sample loses, and is the same order as ``market.TIER_B_WIDENING_FACTOR``.
TIER_B_WIDENING_FACTOR = 1.4

#: Flat penalty applied to the confidence score of a Tier-B competitor
#: prediction, on top of the tier's own lower ceiling: a widened interval is a
#: less useful answer and must not score like a tight one.
TIER_B_CONFIDENCE_PENALTY = 10

#: Minimum number of category-prior observations before a prior is usable at
#: all. A prior fitted on three bids is not a prior. Below this the competitor
#: prediction is suppressed as NO_COMPARABLE_TENDERS even at Tier A/B.
MIN_PRIOR_OBSERVATIONS = 8

#: Below this pooling weight the posterior sigma is floored at the prior sigma.
#: w = 0.75 corresponds to n = 15 at K = 5: until a vendor has that much of its
#: own history it may not claim to be more predictable than its category.
SIGMA_FLOOR_POOLING_WEIGHT = 0.75

#: Absolute floor on posterior sigma in log space. exp(0.02) ~ 2%: the model
#: never claims a competitor's price is pinned to better than a couple of
#: percent, whatever the sample happens to look like.
MIN_SIGMA = 0.02

#: Standard-normal quantile for the 10th/90th percentile (Phi^-1(0.9)).
#: Hardcoded rather than computed: scipy is not a dependency.
Z_P90 = 1.2815515655446004

#: Quantile levels the model emits.
Q_LOW, Q_MID, Q_HIGH = 0.10, 0.50, 0.90

#: Smallest offer/award value treated as real. Zero and negative values are data
#: errors, and ln() of them is undefined.
MIN_PLAUSIBLE_VALUE = 1.0

#: Rounding used when hashing the feature snapshot, so that a snapshot id is
#: stable against float noise but still changes when the evidence changes.
SNAPSHOT_ROUNDING = 6

#: Evidence reference prefix used on explanation factors sourced from a vendor's
#: own observed offers.
EVIDENCE_REF_VENDOR = "vendor"

_SECONDS_PER_DAY = 86400.0


class _Fetcher(Protocol):
    """The slice of an asyncpg connection this module uses."""

    async def fetch(self, query: str, *args: Any) -> Sequence[Any]:  # pragma: no cover
        ...


# --------------------------------------------------------------------------
# SQL. Point-in-time gate is identical to evidence.py's, deliberately.
# --------------------------------------------------------------------------

_KNOWABLE_AT = "COALESCE(a.awarded_at, a.created_at)"

#: A vendor's offers on tenders whose award was knowable before the cutoff,
#: expressed as log ratios against that tender's winning price.
VENDOR_RATIO_SQL = f"""
SELECT ln(o.offer_value::float8 / a.award_value::float8) AS log_ratio,
       {_KNOWABLE_AT} AS knowable_at,
       (t.activity_id IS NOT NULL AND t.activity_id = $3) AS same_activity
FROM offers o
JOIN tenders t ON t.id = o.tender_id
JOIN awards a ON a.tender_id = t.id
WHERE o.vendor_id = $1
  AND o.offer_value >= {MIN_PLAUSIBLE_VALUE}
  AND a.award_value >= {MIN_PLAUSIBLE_VALUE}
  AND {_KNOWABLE_AT} < $2
"""

#: Every vendor's log ratios inside one activity category — the pooling prior.
#: ``$1`` NULL widens it to the whole corpus, which is the documented fallback
#: when a category is too thin to be a prior of its own.
CATEGORY_RATIO_SQL = f"""
SELECT ln(o.offer_value::float8 / a.award_value::float8) AS log_ratio,
       {_KNOWABLE_AT} AS knowable_at
FROM offers o
JOIN tenders t ON t.id = o.tender_id
JOIN awards a ON a.tender_id = t.id
WHERE o.offer_value >= {MIN_PLAUSIBLE_VALUE}
  AND a.award_value >= {MIN_PLAUSIBLE_VALUE}
  AND {_KNOWABLE_AT} < $2
  AND ($1::int IS NULL OR t.activity_id = $1)
"""


# --------------------------------------------------------------------------
# Pure helpers. These carry the test weight: no DB needed to exercise them.
# --------------------------------------------------------------------------

def recency_weight(age_days: float, half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> float:
    """``0.5 ** (age / half_life)``, with negative ages clamped to fresh.

    A record dated slightly in the future (clock skew) counts as 1.0 rather than
    as more than one observation.
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    return 0.5 ** (max(0.0, float(age_days)) / half_life_days)


def _age_days(cutoff: datetime, moment: Any) -> float:
    """Age of ``moment`` at ``cutoff`` in days; 0.0 when it cannot be dated."""
    if not isinstance(moment, datetime):
        return 0.0
    a, b = cutoff, moment
    if a.tzinfo is None or b.tzinfo is None:
        a = a.replace(tzinfo=None)
        b = b.replace(tzinfo=None)
    return max(0.0, (a - b).total_seconds() / _SECONDS_PER_DAY)


def effective_n(weights: Sequence[float]) -> float:
    """Kish effective sample size: ``(sum w)^2 / sum w^2``.

    Equal to the row count when every weight is equal, and smaller when a few
    rows dominate — which is exactly what pooling should react to.
    """
    total = sum(weights)
    squares = sum(w * w for w in weights)
    if squares <= 0.0:
        return 0.0
    return (total * total) / squares


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    """Weighted arithmetic mean; 0.0 for an empty or zero-weight sample."""
    if len(values) != len(weights):
        raise ValueError("values and weights must be the same length")
    total = sum(weights)
    if total <= 0.0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total


def weighted_sd(values: Sequence[float], weights: Sequence[float]) -> float:
    """Reliability-weighted sample standard deviation.

    Denominator is ``W - (sum w^2) / W`` (the weighted analogue of ``n - 1``),
    which goes to zero for a single observation — a single point has no spread,
    and reporting 0.0 there is honest. Callers must not use that 0.0 as a width:
    :func:`shrink` floors it against the prior.
    """
    if len(values) != len(weights):
        raise ValueError("values and weights must be the same length")
    total = sum(weights)
    if total <= 0.0:
        return 0.0
    denominator = total - (sum(w * w for w in weights) / total)
    if denominator <= 0.0:
        return 0.0
    mean = weighted_mean(values, weights)
    numerator = sum(w * (v - mean) ** 2 for v, w in zip(values, weights))
    return math.sqrt(max(0.0, numerator / denominator))


def fit_log_ratios(sample: Sequence[tuple[float, float]]) -> tuple[float, float, float]:
    """Fit ``(mu, sigma, n_eff)`` to a ``[(log_ratio, weight)]`` sample.

    Returns ``(0.0, 0.0, 0.0)`` for an empty sample — a caller must check
    ``n_eff`` rather than reading the zeros as a fitted distribution.
    """
    if not sample:
        return 0.0, 0.0, 0.0
    values = [float(v) for v, _ in sample]
    weights = [float(w) for _, w in sample]
    return weighted_mean(values, weights), weighted_sd(values, weights), effective_n(weights)


def shrink(
    vendor_mu: float,
    vendor_n: float,
    prior_mu: float,
    prior_sigma: float,
    vendor_sigma: float,
) -> tuple[float, float]:
    """Partially pool a vendor's log-ratio fit toward its category prior.

    Returns ``(mu_post, sigma_post)``.

    ``w = vendor_n / (vendor_n + SHRINKAGE_K)`` rises monotonically from 0 to 1,
    so ``mu_post`` moves monotonically from the prior mean toward the vendor's
    own mean as evidence accumulates: at n=2 it is 29% of the way, at n=30 it is
    86% of the way.

    ``sigma_post`` is the standard deviation of the two-component mixture, which
    includes a ``w(1-w)(mu_vendor - mu_prior)^2`` disagreement term so that a
    sparse vendor who looks unlike its category is reported as *less* certain,
    not confidently averaged. Below ``SIGMA_FLOOR_POOLING_WEIGHT`` it is floored
    at the prior sigma: a thinly observed vendor may never look tighter than the
    category it borrowed its shape from. ``MIN_SIGMA`` is an absolute floor.
    """
    if vendor_n < 0:
        raise ValueError("vendor_n must not be negative")
    if prior_sigma < 0 or vendor_sigma < 0:
        raise ValueError("sigmas must not be negative")

    w = vendor_n / (vendor_n + SHRINKAGE_K)
    mu_post = w * vendor_mu + (1.0 - w) * prior_mu

    variance = (
        w * vendor_sigma**2
        + (1.0 - w) * prior_sigma**2
        + w * (1.0 - w) * (vendor_mu - prior_mu) ** 2
    )
    sigma_post = math.sqrt(max(0.0, variance))
    if w < SIGMA_FLOOR_POOLING_WEIGHT:
        sigma_post = max(sigma_post, prior_sigma)
    return mu_post, max(sigma_post, MIN_SIGMA)


def log_quantiles(mu: float, sigma: float, *, widen: float = 1.0) -> tuple[float, float, float]:
    """``(q10, q50, q90)`` of a normal in log space, optionally widened.

    ``widen`` multiplies the deviation from the median, so ordering is preserved
    for any ``widen >= 0`` and the median is untouched.
    """
    if sigma < 0:
        raise ValueError("sigma must not be negative")
    if widen < 0:
        raise ValueError("widen must not be negative")
    spread = widen * Z_P90 * sigma
    return mu - spread, mu, mu + spread


def to_sar(log_quantile_triple: tuple[float, float, float], baseline: float) -> Quantiles:
    """Back-transform log ratios to SAR against a market baseline.

    ``exp`` is strictly increasing and ``baseline > 0``, so the p10 <= p50 <= p90
    ordering of the log triple survives the transform exactly.
    """
    if baseline <= 0:
        raise ValueError("baseline must be positive")
    low, mid, high = log_quantile_triple
    return Quantiles(
        p10=baseline * math.exp(low),
        p50=baseline * math.exp(mid),
        p90=baseline * math.exp(high),
    ).validate()


def _normal_cdf(z: float) -> float:
    """Phi(z) via ``math.erf`` — stdlib only, deterministic, no scipy."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def implied_sigma(prediction: PricePrediction) -> float | None:
    """Recover the log-space sigma from an emitted p10/p90 pair.

    ``ln(p90 / p10) = 2 * Z_P90 * sigma`` for the lognormal this module emits, so
    the fitted shape can be read back off the contract object without carrying a
    private field. Returns None when the prediction has no usable band.
    """
    if prediction.is_suppressed:
        return None
    p10, p90 = prediction.p10, prediction.p90
    if p10 is None or p90 is None or p10 <= 0 or p90 <= 0:
        return None
    return math.log(p90 / p10) / (2.0 * Z_P90)


def undercut_risk(user_bid: float, competitor: PricePrediction) -> float | None:
    """P(competitor bids below ``user_bid``), from the competitor's lognormal.

    Returns None when the competitor prediction is suppressed — there is no
    number to reason from, and inventing one here would route around the
    suppression gate. Returns 0.0 for a non-positive bid (nothing bids below
    zero) and is monotonically **decreasing** in the competitor's p50: the
    higher the competitor is expected to price, the less likely it undercuts.

    This is a statement about a *modelled distribution*, not a claim about what
    the competitor will do. It must be rendered as متوقع, never as fact.
    """
    if competitor.is_suppressed:
        return None
    p50 = competitor.p50
    if p50 is None or p50 <= 0:
        return None
    if user_bid <= 0:
        return 0.0
    sigma = implied_sigma(competitor)
    if sigma is None or sigma <= 0:
        # Degenerate band: a point mass at p50.
        return 1.0 if user_bid > p50 else 0.0
    return _normal_cdf(math.log(user_bid / p50) / sigma)


def _snapshot_id(
    *, vendor_id: int, cutoff: datetime, sample: Sequence[tuple[float, float]], prior_n: int
) -> str:
    """Deterministic id for the exact evidence a prediction was built from.

    Hashes the vendor, the cutoff, the rounded sample and the prior size, so two
    runs over unchanged data produce the same id and any change to the evidence
    produces a different one.
    """
    parts = [MODEL_VERSION, str(vendor_id), cutoff.isoformat(), str(prior_n)]
    parts.extend(
        f"{round(float(v), SNAPSHOT_ROUNDING)}:{round(float(w), SNAPSHOT_ROUNDING)}"
        for v, w in sorted(sample)
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# DB-touching functions.
# --------------------------------------------------------------------------

async def competitor_ratio_sample(
    conn: _Fetcher,
    *,
    vendor_id: int,
    activity_id: int | None,
    as_of: datetime | None = None,
) -> list[tuple[float, float]]:
    """``[(log_ratio, recency_weight)]`` for one vendor, before ``as_of``.

    Each row is one observed offer expressed as ``ln(offer / winning price)`` of
    the tender it was made on. The weight combines recency decay with a
    down-weight for observations outside ``activity_id`` — see
    ``CROSS_ACTIVITY_WEIGHT``. When ``activity_id`` is None no category is known,
    so nothing is down-weighted.

    The target tender is excluded implicitly: its own award is not knowable
    before its own cutoff, so the point-in-time gate drops it.
    """
    cutoff = _resolve_as_of({}, as_of)
    rows = await conn.fetch(VENDOR_RATIO_SQL, vendor_id, cutoff, activity_id)

    sample: list[tuple[float, float]] = []
    for row in rows:
        log_ratio = row["log_ratio"]
        if log_ratio is None or math.isnan(float(log_ratio)):
            continue
        weight = recency_weight(_age_days(cutoff, row["knowable_at"]))
        if activity_id is not None and not row["same_activity"]:
            weight *= CROSS_ACTIVITY_WEIGHT
        sample.append((float(log_ratio), weight))
    return sample


async def category_prior_sample(
    conn: _Fetcher,
    *,
    activity_id: int | None,
    as_of: datetime | None = None,
) -> list[tuple[float, float]]:
    """``[(log_ratio, recency_weight)]`` for a whole activity category.

    Falls back to the entire corpus when the category yields fewer than
    ``MIN_PRIOR_OBSERVATIONS`` rows: a prior fitted on three bids is worse than
    a broad one, and the fallback is visible to the caller as the sample size it
    gets back. Returns the category sample unchanged when ``activity_id`` is
    None (the query is already corpus-wide in that case).
    """
    cutoff = _resolve_as_of({}, as_of)
    rows = await conn.fetch(CATEGORY_RATIO_SQL, activity_id, cutoff)
    if activity_id is not None and len(rows) < MIN_PRIOR_OBSERVATIONS:
        rows = await conn.fetch(CATEGORY_RATIO_SQL, None, cutoff)

    sample: list[tuple[float, float]] = []
    for row in rows:
        log_ratio = row["log_ratio"]
        if log_ratio is None or math.isnan(float(log_ratio)):
            continue
        sample.append((float(log_ratio), recency_weight(_age_days(cutoff, row["knowable_at"]))))
    return sample


async def category_prior(
    conn: _Fetcher,
    *,
    activity_id: int | None,
    as_of: datetime | None = None,
) -> tuple[float, float]:
    """``(mu, sigma)`` of log bid ratios in one activity category.

    Convenience wrapper over :func:`category_prior_sample` for callers that only
    want the fitted pair. ``competitor_quantiles`` deliberately uses the sample
    function instead, because it must gate on how many observations the prior
    rests on and that count is not recoverable from ``(mu, sigma)``.
    """
    sample = await category_prior_sample(conn, activity_id=activity_id, as_of=as_of)
    mu, sigma, _ = fit_log_ratios(sample)
    return mu, sigma


def _resolve_as_of(tender: dict[str, Any], as_of: datetime | None) -> datetime:
    """Delegate to ``evidence.resolve_as_of`` (local import: a patchable seam)."""
    from . import evidence as evidence_mod

    return evidence_mod.resolve_as_of(tender, as_of)


def _factors(
    *,
    tier: EvidenceTier,
    observation_count: int,
    vendor_n_eff: float,
    prior_n: int,
    pooling_weight: float,
    vendor_mu: float,
    prior_mu: float,
    baseline: float,
    widened: bool,
) -> list[ExplanationFactor]:
    """The reasons behind the number, each tagged observed / derived / predicted.

    ``kind`` drives the house display rule (مُلاحظ / متوقع / إدخالك), so a raw
    count of the vendor's bids and the pooled model output must not be tagged
    alike.
    """
    direction = "increases" if vendor_mu >= prior_mu else "decreases"
    factors = [
        ExplanationFactor(
            name="vendor_observed_offers",
            direction="neutral",
            weight=float(observation_count),
            kind="observed",
            detail=(
                f"{observation_count} recorded offers by this vendor on tenders already "
                f"awarded before the cutoff (recency-weighted: {vendor_n_eff:.2f})"
            ),
            evidence_ref=EVIDENCE_REF_VENDOR,
        ),
        ExplanationFactor(
            name="category_prior_size",
            direction="neutral",
            weight=float(prior_n),
            kind="observed",
            detail=f"{prior_n} offers form the category prior this vendor is pooled toward",
        ),
        ExplanationFactor(
            name="pooling_weight",
            direction="neutral",
            weight=round(pooling_weight, 4),
            kind="derived",
            detail=(
                f"{pooling_weight:.0%} of the fit comes from this vendor's own history, "
                f"{1 - pooling_weight:.0%} from the category prior (K={SHRINKAGE_K:g})"
            ),
        ),
        ExplanationFactor(
            name="vendor_vs_category_ratio",
            direction=direction,
            weight=round(vendor_mu - prior_mu, 4),
            kind="derived",
            detail=(
                f"this vendor's observed bids run {math.exp(vendor_mu):.2f}x the winning "
                f"price against a category average of {math.exp(prior_mu):.2f}x"
            ),
        ),
        ExplanationFactor(
            name="market_baseline_p50",
            direction="neutral",
            weight=float(baseline),
            kind="predicted",
            detail="the ratio model is scaled by the market model's predicted P50 award value",
        ),
        ExplanationFactor(
            name="evidence_tier",
            direction="neutral",
            weight=0.0,
            kind="derived",
            detail=(
                f"tier {tier.value}"
                + (
                    f"; interval widened {TIER_B_WIDENING_FACTOR:g}x and confidence reduced "
                    f"because the evidence is moderate"
                    if widened
                    else ""
                )
            ),
        ),
    ]
    return factors


async def competitor_quantiles(
    conn: _Fetcher,
    *,
    vendor_id: int,
    tender: dict[str, Any],
    as_of: datetime | None = None,
    market: PricePrediction | None = None,
) -> PricePrediction:
    """P10/P50/P90 in SAR for one named vendor's likely bid on one tender.

    Evidence is consulted **first** and is a veto: Tier C or D returns a
    suppressed ``PricePrediction`` with ``INSUFFICIENT_EVIDENCE`` and no numbers
    at all. There is no reduced-quality competitor answer below Tier B — the
    fallback the UI should show is the market range.

    ``market`` is the MARKET-scope prediction supplying the SAR baseline; it is
    fetched from ``p2w.market.market_quantiles`` when not passed. If that is
    itself suppressed, its reason propagates: a competitor price cannot be more
    supportable than the baseline it is scaled by.
    """
    from . import evidence as evidence_mod
    from . import market as market_mod

    cutoff = _resolve_as_of(tender, as_of)
    tender_id = int(tender["id"])
    activity_id = tender.get("activity_id")

    ev = await evidence_mod.competitor_evidence(
        conn, vendor_id=vendor_id, tender=tender, as_of=cutoff
    )

    def _suppress(
        reason: SuppressionReason, *, factors: list[ExplanationFactor] | None = None
    ) -> PricePrediction:
        return PricePrediction.suppressed(
            tender_id=tender_id,
            scope=PredictionScope.COMPETITOR,
            reason=reason,
            subject_id=vendor_id,
            evidence_count=ev.observation_count,
            evidence_tier=ev.tier,
            similarity_confidence=ev.similarity_confidence,
            data_freshness_score=ev.freshness,
            explanation_factors=factors or [],
        )

    # --- Gate 1: the tier ladder. Hard veto, no fallback. -------------------
    if not ev.tier.allows_competitor_prediction:
        return _suppress(
            SuppressionReason.INSUFFICIENT_EVIDENCE,
            factors=[
                ExplanationFactor(
                    name="evidence_gate",
                    direction="neutral",
                    weight=float(ev.observation_count),
                    kind="observed",
                    detail=(
                        f"tier {ev.tier.value} bars any competitor-level price"
                        + (f" (gate: {ev.suppression.value})" if ev.suppression else "")
                    ),
                    evidence_ref=EVIDENCE_REF_VENDOR,
                )
            ],
        )

    # --- Gate 2: the SAR baseline. ----------------------------------------
    if market is None:
        market = await market_mod.market_quantiles(conn, tender=tender, as_of=cutoff)
    if market.is_suppressed or market.p50 is None or market.p50 <= 0:
        return _suppress(
            market.suppression_reason or SuppressionReason.MODEL_UNAVAILABLE,
            factors=[
                ExplanationFactor(
                    name="market_baseline_unavailable",
                    direction="neutral",
                    weight=0.0,
                    kind="derived",
                    detail="no supportable market P50 to scale the bid-ratio model by",
                )
            ],
        )
    baseline = float(market.p50)

    # --- Gate 3: the samples. ---------------------------------------------
    vendor_sample = await competitor_ratio_sample(
        conn, vendor_id=vendor_id, activity_id=activity_id, as_of=cutoff
    )
    if not vendor_sample:
        return _suppress(SuppressionReason.INSUFFICIENT_EVIDENCE)

    prior_sample = await category_prior_sample(conn, activity_id=activity_id, as_of=cutoff)
    if len(prior_sample) < MIN_PRIOR_OBSERVATIONS:
        return _suppress(SuppressionReason.NO_COMPARABLE_TENDERS)

    # --- Fit, pool, transform. --------------------------------------------
    vendor_mu, vendor_sigma, vendor_n = fit_log_ratios(vendor_sample)
    prior_mu, prior_sigma, _ = fit_log_ratios(prior_sample)
    mu_post, sigma_post = shrink(vendor_mu, vendor_n, prior_mu, prior_sigma, vendor_sigma)

    widened = ev.tier.requires_widened_interval
    quantiles = to_sar(
        log_quantiles(mu_post, sigma_post, widen=TIER_B_WIDENING_FACTOR if widened else 1.0),
        baseline,
    )

    width_ratio = quantiles.width / quantiles.p50 if quantiles.p50 > 0 else 0.0
    confidence = evidence_mod.confidence_score(
        tier=ev.tier,
        evidence_count=ev.effective_n,
        freshness=ev.freshness,
        similarity_confidence=ev.similarity_confidence,
        interval_width_ratio=width_ratio,
    )
    if widened:
        confidence = max(0, confidence - TIER_B_CONFIDENCE_PENALTY)

    pooling_weight = vendor_n / (vendor_n + SHRINKAGE_K)
    return PricePrediction(
        tender_id=tender_id,
        prediction_scope=PredictionScope.COMPETITOR,
        subject_id=vendor_id,
        p10=quantiles.p10,
        p50=quantiles.p50,
        p90=quantiles.p90,
        # Lognormal mean, which sits above the median whenever sigma > 0. Kept
        # distinct from p50 on purpose: the two answer different questions.
        expected_value=baseline * math.exp(mu_post + 0.5 * sigma_post**2),
        win_probability=None,  # not this model's question
        confidence_score=confidence,
        similarity_confidence=ev.similarity_confidence,
        data_freshness_score=ev.freshness,
        evidence_count=ev.observation_count,
        evidence_tier=ev.tier,
        model_version=MODEL_VERSION,
        feature_snapshot_id=_snapshot_id(
            vendor_id=vendor_id,
            cutoff=cutoff,
            sample=vendor_sample,
            prior_n=len(prior_sample),
        ),
        seed=None,  # closed-form: no Monte Carlo, nothing random to reproduce
        explanation_factors=_factors(
            tier=ev.tier,
            observation_count=ev.observation_count,
            vendor_n_eff=vendor_n,
            prior_n=len(prior_sample),
            pooling_weight=pooling_weight,
            vendor_mu=vendor_mu,
            prior_mu=prior_mu,
            baseline=baseline,
            widened=widened,
        ),
    )


__all__ = [
    "CROSS_ACTIVITY_WEIGHT",
    "MIN_PRIOR_OBSERVATIONS",
    "MIN_SIGMA",
    "RECENCY_HALF_LIFE_DAYS",
    "SHRINKAGE_K",
    "SIGMA_FLOOR_POOLING_WEIGHT",
    "TIER_B_CONFIDENCE_PENALTY",
    "TIER_B_WIDENING_FACTOR",
    "Z_P90",
    "category_prior",
    "category_prior_sample",
    "competitor_quantiles",
    "competitor_ratio_sample",
    "effective_n",
    "fit_log_ratios",
    "implied_sigma",
    "log_quantiles",
    "recency_weight",
    "shrink",
    "to_sar",
    "undercut_risk",
    "weighted_mean",
    "weighted_sd",
]
