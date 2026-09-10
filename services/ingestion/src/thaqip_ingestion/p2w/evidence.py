"""Evidence tiering, confidence scoring and suppression gates.

This module decides **what the system is allowed to say**. Every other P2W
producer calls it before emitting a number, and takes ``suppression`` as a veto
rather than a hint.

The model
--------
Three questions are answered separately, because they fail separately:

1. *How much comparable market evidence exists?* — awarded tenders in the same
   activity (or, failing that, the same agency) whose award became knowable
   **strictly before** the point in time we are predicting for.
2. *How much history do we have on this particular vendor?* — the vendor's own
   offers on those already-decided tenders, counted with recency decay so that a
   bid from three years ago does not count the same as one from last quarter.
3. *How comparable are the comparables, and how stale are they?* — a similarity
   confidence and a freshness score, both 0-100.

:func:`classify_tier` turns those into an :class:`EvidenceTier` plus, when a
competitor-level price may not be shown, a machine-readable
:class:`SuppressionReason`. :func:`confidence_score` turns them into a single
0-100 number for display.

Point-in-time correctness
-------------------------
A comparable counts only if the fact was knowable before ``as_of``. The award
date we would want (``awards.awarded_at``) is NULL for every row in the current
corpus, so the knowability timestamp falls back to ``awards.created_at`` — the
moment we ingested the award. That is provably no earlier than the real award
date, so the fallback can only *exclude* comparables we were entitled to use; it
can never leak a fact from the future. ``tenders.offers_opening_date`` is
deliberately **not** used as an award-knowability proxy: bids open before the
award is decided, so it would date the fact earlier than it was knowable.

Honest limitations
------------------
* With the present corpus (307 awarded tenders, most vendors with 1-4 observed
  offers, ``awarded_at`` universally NULL and ``offers_opening_date`` present on
  2 tenders) the overwhelmingly common outcome of this module is Tier C or D,
  i.e. *the competitor price is suppressed*. That is the correct output, not a
  gap in the implementation.
* Similarity here is structural (activity match, agency overlap), not semantic.
  Two road-maintenance contracts of wildly different size in the same activity
  look equally similar to this module. A future text/BOQ similarity model should
  replace :func:`_similarity_confidence`, not wrap it.
* Freshness is measured on ingestion-time proxies and therefore currently
  OVER-states recency: every award in the corpus was ingested inside one week, so
  ``created_at`` dates a years-old award as days old and almost everything
  scores 99-100. The staleness gates are, in practice, inert today. They are not
  decoration — they will bite as soon as real ``awarded_at`` values land — but no
  one should read a high freshness score off this corpus as evidence of recency.
  The same artifact flattens the half-life decay, so ``effective_n`` is presently
  close to the raw ``observation_count``.
* ``effective_n`` counts a vendor's offers regardless of whether they were in
  this tender's activity or agency: it is a recency-weighted volume, not a
  relevance-weighted one. ``activity_overlap_count`` and ``agency_overlap_count``
  are returned alongside it precisely so a downstream quantile model can apply
  the relevance judgement this module deliberately does not make.
* ``confidence_score`` is a statement about the *evidence*, never about the odds
  of winning. See its docstring.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .contracts import (
    EvidenceTier,
    SuppressionReason,
    freshness_score,
)

__all__ = [
    "CompetitorEvidence",
    "MarketEvidence",
    "classify_tier",
    "competitor_evidence",
    "confidence_score",
    "decayed_count",
    "market_evidence",
    "median",
]


# --------------------------------------------------------------------------
# Tunable constants. Every threshold below is calibrated against the measured
# corpus: 1670 tenders, 307 awarded, 202 of them multi-bidder, 529 vendors of
# which 418 appear exactly once and only 17 appear 8+ times.
# --------------------------------------------------------------------------

#: Half-life for recency weighting of a vendor's past offers. One year: Saudi
#: public procurement runs on annual budget cycles, so a bid from the previous
#: cycle is worth about half a bid from the current one when reasoning about a
#: vendor's current pricing posture.
HALF_LIFE_DAYS = 365.0

#: Tier A — a competitor-level price may be stated with a normal interval.
#: 8 effective observations is the point at which a per-vendor quantile stops
#: being dominated by a single bid (only 17 of 529 vendors clear it today, which
#: is exactly the intended rarity).
TIER_A_MIN_EFFECTIVE_N = 8.0
#: 12 comparable awarded tenders: the largest activity in the corpus (activity
#: 111) has 129, so this is reachable there and nowhere near reachable in the
#: long tail — again the intended selectivity.
TIER_A_MIN_COMPARABLES = 12
#: Below 60 the comparable set is mostly cross-agency, and agency behaviour is
#: the single strongest driver of award level in this market.
TIER_A_MIN_SIMILARITY = 60
#: 50 ⇒ median evidence age of one year or less (freshness decays linearly to 0
#: over 730 days). Older than that and a stated competitor price is a guess.
TIER_A_MIN_FRESHNESS = 50

#: Tier B — a competitor range may be shown, but WIDENED (see
#: ``EvidenceTier.requires_widened_interval``). 4 effective observations is the
#: minimum from which a spread, as opposed to a point, can be argued at all.
TIER_B_MIN_EFFECTIVE_N = 4.0
TIER_B_MIN_COMPARABLES = 8

#: Tier C — market-level statements only; the competitor price is suppressed.
#: 5 comparables is the smallest set from which a p10/p50/p90 is more than an
#: ordering of the raw points.
TIER_C_MIN_COMPARABLES = 5

#: Hard gates. These override the ladder: they describe evidence that is
#: unusable in kind, not merely thin in volume.
#: Freshness below 15 means a median age past ~620 days — nearly two budget
#: cycles, by which point award levels in this market have re-based.
HARD_STALE_FLOOR_FRESHNESS = 15
#: Similarity below 25 means the "comparables" share neither activity mix nor
#: agency in any meaningful proportion; counting them would launder noise.
HARD_LOW_SIMILARITY_FLOOR = 25

#: Similarity components. An activity match is the baseline notion of "same kind
#: of work"; sharing the buying agency on top of that is what makes a comparable
#: genuinely comparable, so the two are weighted 55/45.
SIM_ACTIVITY_BASE = 55
SIM_AGENCY_BONUS = 45
#: When the subject tender has no activity and comparables were matched on the
#: agency alone, likeness of *work* is unknown — capped well under the Tier A bar.
SIM_AGENCY_ONLY = 45

#: Confidence composition. Base is what the tier alone buys; the cap is what the
#: tier alone permits however good everything else looks. Both are ordered
#: A > B > C > D, which is what makes the score monotone in tier.
TIER_CONFIDENCE_BASE: dict[EvidenceTier, int] = {
    EvidenceTier.A: 55,
    EvidenceTier.B: 40,
    EvidenceTier.C: 25,
    EvidenceTier.D: 5,
}
TIER_CONFIDENCE_CAP: dict[EvidenceTier, int] = {
    EvidenceTier.A: 100,
    EvidenceTier.B: 80,
    EvidenceTier.C: 55,
    EvidenceTier.D: 25,
}
#: Weight of raw evidence volume, applied through a saturating curve: the step
#: from 2 to 6 observations matters far more than the step from 40 to 44.
CONFIDENCE_EVIDENCE_WEIGHT = 20.0
#: Observations at which the evidence component reaches ~63% of its weight.
CONFIDENCE_EVIDENCE_SATURATION_N = 10.0
CONFIDENCE_FRESHNESS_WEIGHT = 15.0
CONFIDENCE_SIMILARITY_WEIGHT = 10.0
#: A p10..p90 band worth the whole median is uninformative; that is where the
#: full width penalty lands.
CONFIDENCE_WIDTH_WEIGHT = 15.0
CONFIDENCE_WIDTH_RATIO_CAP = 1.0


# --------------------------------------------------------------------------
# SQL. Kept as module constants so the point-in-time predicate is auditable in
# one place rather than reconstructed per call site.
# --------------------------------------------------------------------------

#: See the module docstring: awarded_at where the source gave one, otherwise the
#: ingestion timestamp, which is conservatively late and never early.
_KNOWABLE_AT = "COALESCE(a.awarded_at, a.created_at)"

MARKET_BY_ACTIVITY_SQL = f"""
SELECT {_KNOWABLE_AT} AS knowable_at,
       (t.agency_id IS NOT NULL AND t.agency_id = $4) AS same_agency
FROM awards a
JOIN tenders t ON t.id = a.tender_id
WHERE t.id <> $1
  AND t.activity_id = $3
  AND a.award_value IS NOT NULL
  AND {_KNOWABLE_AT} < $2
"""

MARKET_BY_AGENCY_SQL = f"""
SELECT {_KNOWABLE_AT} AS knowable_at,
       TRUE AS same_agency
FROM awards a
JOIN tenders t ON t.id = a.tender_id
WHERE t.id <> $1
  AND t.agency_id = $4
  AND a.award_value IS NOT NULL
  AND {_KNOWABLE_AT} < $2
"""

COMPETITOR_OFFERS_SQL = f"""
SELECT {_KNOWABLE_AT} AS knowable_at,
       (t.agency_id IS NOT NULL AND t.agency_id = $4) AS same_agency,
       (t.activity_id IS NOT NULL AND t.activity_id = $3) AS same_activity
FROM offers o
JOIN tenders t ON t.id = o.tender_id
JOIN awards a ON a.tender_id = t.id
WHERE o.vendor_id = $5
  AND t.id <> $1
  AND o.offer_value IS NOT NULL
  AND {_KNOWABLE_AT} < $2
"""


class _Fetcher(Protocol):
    """The slice of an asyncpg connection this module uses."""

    async def fetch(self, query: str, *args: Any) -> Sequence[Any]:  # pragma: no cover
        ...


@dataclass(frozen=True)
class MarketEvidence:
    """What the market as a whole supports for one tender at one point in time.

    ``suppression`` is populated only when even a *market* statement is
    disallowed (Tier D). A Tier C market evidence object has ``suppression is
    None`` while still forbidding any competitor-level claim — ask the tier, via
    ``tier.allows_competitor_prediction``, rather than reading None as consent.
    """

    comparable_count: int
    median_age_days: float
    freshness: int
    similarity_confidence: int
    tier: EvidenceTier
    suppression: SuppressionReason | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparable_count": self.comparable_count,
            "median_age_days": self.median_age_days,
            "freshness": self.freshness,
            "similarity_confidence": self.similarity_confidence,
            "tier": self.tier.value,
            "suppression": self.suppression.value if self.suppression else None,
        }


@dataclass(frozen=True)
class CompetitorEvidence:
    """What is known about one vendor's bidding, on top of the market picture.

    ``effective_n`` is the recency-weighted observation count and is the number
    the tier ladder actually reads; ``observation_count`` is the raw count and is
    what the UI shows as an observed fact.
    """

    comparable_count: int
    median_age_days: float
    freshness: int
    similarity_confidence: int
    tier: EvidenceTier
    suppression: SuppressionReason | None
    observation_count: int
    agency_overlap_count: int
    activity_overlap_count: int
    effective_n: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparable_count": self.comparable_count,
            "median_age_days": self.median_age_days,
            "freshness": self.freshness,
            "similarity_confidence": self.similarity_confidence,
            "tier": self.tier.value,
            "suppression": self.suppression.value if self.suppression else None,
            "observation_count": self.observation_count,
            "agency_overlap_count": self.agency_overlap_count,
            "activity_overlap_count": self.activity_overlap_count,
            "effective_n": self.effective_n,
        }


# --------------------------------------------------------------------------
# Pure helpers — no DB, so they carry the bulk of the test weight.
# --------------------------------------------------------------------------

def median(values: Sequence[float]) -> float:
    """Median of a sequence; 0.0 for an empty one.

    Pure python on purpose: numpy is not a dependency of this service and a
    six-line median is cheaper than adding one.
    """
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def decayed_count(ages_days: Sequence[float], half_life_days: float = HALF_LIFE_DAYS) -> float:
    """Recency-weighted observation count: sum of ``0.5 ** (age / half_life)``.

    An observation made today counts 1.0, one a half-life old counts 0.5. Ages
    are clamped at zero so a record dated slightly in the future (clock skew)
    counts as fresh rather than as more than one observation.
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    total = 0.0
    for age in ages_days:
        total += 0.5 ** (max(0.0, float(age)) / half_life_days)
    return round(total, 6)


def _coerce_utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than guessing a local zone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _age_days(as_of: datetime, moment: datetime) -> float:
    return (_coerce_utc(as_of) - _coerce_utc(moment)).total_seconds() / 86400.0


def resolve_as_of(tender: dict[str, Any], as_of: datetime | None = None) -> datetime:
    """The instant we are predicting *for*, and the point-in-time cutoff.

    Explicit ``as_of`` wins. Otherwise the tender's own offers-opening date is
    the moment its price is decided; failing that the last date offers may be
    submitted; failing both, now — which is the only choice that does not invent
    a date, and which for a live tender is also the honest one.
    """
    if as_of is not None:
        return _coerce_utc(as_of)
    for key in ("offers_opening_date", "last_offer_date"):
        value = tender.get(key)
        if isinstance(value, datetime):
            return _coerce_utc(value)
    return datetime.now(UTC)


def _similarity_confidence(
    *, comparable_count: int, same_agency_count: int, matched_on_activity: bool
) -> int:
    """0-100 structural likeness of the comparable set to the subject tender.

    Activity match is the base; the share of comparables that also share the
    buying agency earns the rest. When only the agency could be matched (the
    subject tender has no activity), likeness of work is unknown and the score is
    capped at SIM_AGENCY_ONLY.
    """
    if comparable_count <= 0:
        return 0
    if not matched_on_activity:
        return SIM_AGENCY_ONLY
    agency_share = same_agency_count / comparable_count
    return round(SIM_ACTIVITY_BASE + SIM_AGENCY_BONUS * agency_share)


def classify_tier(
    *,
    comparable_count: int,
    effective_competitor_n: float,
    similarity_confidence: int,
    freshness: int,
) -> tuple[EvidenceTier, SuppressionReason | None]:
    """Map evidence quantities to a tier and, when competitor claims are barred,
    the reason.

    The returned reason is the **competitor** suppression reason: it is None for
    Tier A and B (where a competitor price may be stated) and always non-None for
    Tier C and D. Callers producing a market-level number should consult
    ``tier.allows_market_prediction`` instead.

    Order of decision, and why: the hard gates come first because they describe
    evidence that is unusable in *kind* — none at all, ancient, or not actually
    comparable — and no amount of volume repairs that. Only then does the volume
    ladder run, and a rung missed there is reported as INSUFFICIENT_EVIDENCE.
    """
    if comparable_count < 0:
        raise ValueError("comparable_count must not be negative")
    if effective_competitor_n < 0:
        raise ValueError("effective_competitor_n must not be negative")
    for name, value in (
        ("similarity_confidence", similarity_confidence),
        ("freshness", freshness),
    ):
        if not 0 <= value <= 100:
            raise ValueError(f"{name} must be in [0,100], got {value}")

    # Hard gates.
    if comparable_count == 0:
        return EvidenceTier.D, SuppressionReason.NO_COMPARABLE_TENDERS
    if freshness < HARD_STALE_FLOOR_FRESHNESS:
        return EvidenceTier.D, SuppressionReason.STALE_DATA
    if similarity_confidence < HARD_LOW_SIMILARITY_FLOOR:
        return EvidenceTier.D, SuppressionReason.LOW_SIMILARITY

    # Volume ladder.
    if (
        effective_competitor_n >= TIER_A_MIN_EFFECTIVE_N
        and comparable_count >= TIER_A_MIN_COMPARABLES
        and similarity_confidence >= TIER_A_MIN_SIMILARITY
        and freshness >= TIER_A_MIN_FRESHNESS
    ):
        return EvidenceTier.A, None
    if (
        effective_competitor_n >= TIER_B_MIN_EFFECTIVE_N
        and comparable_count >= TIER_B_MIN_COMPARABLES
    ):
        return EvidenceTier.B, None
    if comparable_count >= TIER_C_MIN_COMPARABLES:
        return EvidenceTier.C, SuppressionReason.INSUFFICIENT_EVIDENCE
    return EvidenceTier.D, SuppressionReason.INSUFFICIENT_EVIDENCE


def confidence_score(
    *,
    tier: EvidenceTier,
    evidence_count: float,
    freshness: int,
    similarity_confidence: int,
    interval_width_ratio: float,
) -> int:
    """0-100 score for *how well supported* a number is.

    **This is not a win probability.** It says nothing about the odds of winning
    the tender, and must never be rendered as if it did. A confidence of 90 on a
    prediction that the market will land at 4.1M SAR means the evidence behind
    that range is strong; the bid's chance of winning is a separate quantity
    produced by a separate model.

    Monotonicity is a contract, not an accident: the score is non-decreasing in
    ``evidence_count``, ``freshness``, ``similarity_confidence`` and in tier
    strength (D < C < B < A), and non-increasing in ``interval_width_ratio``.
    Each component is individually monotone and they are combined by addition,
    a clamp and a min, all of which preserve it.

    ``interval_width_ratio`` is (p90 - p10) / p50 — the band's width relative to
    its own centre. 0 for a suppressed prediction that has no band.
    """
    if evidence_count < 0:
        raise ValueError("evidence_count must not be negative")
    if interval_width_ratio < 0:
        raise ValueError("interval_width_ratio must not be negative")
    for name, value in (
        ("similarity_confidence", similarity_confidence),
        ("freshness", freshness),
    ):
        if not 0 <= value <= 100:
            raise ValueError(f"{name} must be in [0,100], got {value}")
    if not isinstance(tier, EvidenceTier):
        tier = EvidenceTier(tier)

    base = TIER_CONFIDENCE_BASE[tier]
    # Saturating: 1 - 0.5 ** (n / saturation) rises fast then flattens, and is
    # bounded by 1 so a vendor with 200 observations cannot buy its way past the
    # tier cap.
    evidence_component = CONFIDENCE_EVIDENCE_WEIGHT * (
        1.0 - 0.5 ** (float(evidence_count) / CONFIDENCE_EVIDENCE_SATURATION_N)
    )
    freshness_component = CONFIDENCE_FRESHNESS_WEIGHT * (freshness / 100.0)
    similarity_component = CONFIDENCE_SIMILARITY_WEIGHT * (similarity_confidence / 100.0)
    width_penalty = CONFIDENCE_WIDTH_WEIGHT * (
        min(float(interval_width_ratio), CONFIDENCE_WIDTH_RATIO_CAP)
        / CONFIDENCE_WIDTH_RATIO_CAP
    )

    raw = base + evidence_component + freshness_component + similarity_component - width_penalty
    bounded = max(0.0, min(float(TIER_CONFIDENCE_CAP[tier]), raw))
    return round(bounded)


# --------------------------------------------------------------------------
# DB-touching entry points.
# --------------------------------------------------------------------------

async def market_evidence(
    conn: _Fetcher,
    *,
    tender: dict[str, Any],
    as_of: datetime | None = None,
) -> MarketEvidence:
    """Count and grade the awarded comparables knowable before ``as_of``.

    Comparables are awarded tenders in the same activity; when the subject
    tender has no activity, the same agency is used instead and the similarity
    score is capped accordingly. A tender with neither has no comparable set at
    all and lands in Tier D with NO_COMPARABLE_TENDERS — which is the truth, not
    a failure.
    """
    cutoff = resolve_as_of(tender, as_of)
    tender_id = tender.get("id")
    activity_id = tender.get("activity_id")
    agency_id = tender.get("agency_id")

    matched_on_activity = activity_id is not None
    if matched_on_activity:
        sql = MARKET_BY_ACTIVITY_SQL
    elif agency_id is not None:
        sql = MARKET_BY_AGENCY_SQL
    else:
        return _grade_market(
            ages=[], same_agency_count=0, matched_on_activity=False
        )

    rows = await conn.fetch(sql, tender_id, cutoff, activity_id, agency_id)
    ages = [_age_days(cutoff, row["knowable_at"]) for row in rows]
    same_agency_count = sum(1 for row in rows if row["same_agency"])
    return _grade_market(
        ages=ages,
        same_agency_count=same_agency_count,
        matched_on_activity=matched_on_activity,
    )


def _grade_market(
    *, ages: list[float], same_agency_count: int, matched_on_activity: bool
) -> MarketEvidence:
    comparable_count = len(ages)
    median_age = round(median(ages), 3)
    fresh = freshness_score(median_age) if comparable_count else 0
    similarity = _similarity_confidence(
        comparable_count=comparable_count,
        same_agency_count=same_agency_count,
        matched_on_activity=matched_on_activity,
    )
    # effective_competitor_n=0: market evidence on its own can never license a
    # competitor-level claim, so the ladder is entered with no vendor history.
    tier, reason = classify_tier(
        comparable_count=comparable_count,
        effective_competitor_n=0.0,
        similarity_confidence=similarity,
        freshness=fresh,
    )
    return MarketEvidence(
        comparable_count=comparable_count,
        median_age_days=median_age,
        freshness=fresh,
        similarity_confidence=similarity,
        tier=tier,
        suppression=None if tier.allows_market_prediction else reason,
    )


async def competitor_evidence(
    conn: _Fetcher,
    *,
    vendor_id: int,
    tender: dict[str, Any],
    as_of: datetime | None = None,
) -> CompetitorEvidence:
    """Grade what is known about one vendor's bidding for this tender.

    The vendor's observations are its offers on tenders that had already been
    awarded before ``as_of`` — the award is what makes the offer table visible,
    so joining it is both the correct point-in-time gate and the reason the
    counts are small.

    Freshness is measured on the vendor's own observations when it has any (that
    is the evidence a competitor claim would rest on) and falls back to the
    market comparables otherwise.
    """
    cutoff = resolve_as_of(tender, as_of)
    market = await market_evidence(conn, tender=tender, as_of=cutoff)

    rows = await conn.fetch(
        COMPETITOR_OFFERS_SQL,
        tender.get("id"),
        cutoff,
        tender.get("activity_id"),
        tender.get("agency_id"),
        vendor_id,
    )
    ages = [_age_days(cutoff, row["knowable_at"]) for row in rows]
    observation_count = len(rows)
    agency_overlap = sum(1 for row in rows if row["same_agency"])
    activity_overlap = sum(1 for row in rows if row["same_activity"])
    effective_n = decayed_count(ages)

    median_age = round(median(ages), 3) if ages else market.median_age_days
    fresh = freshness_score(median_age) if ages else market.freshness

    tier, reason = classify_tier(
        comparable_count=market.comparable_count,
        effective_competitor_n=effective_n,
        similarity_confidence=market.similarity_confidence,
        freshness=fresh,
    )
    return CompetitorEvidence(
        comparable_count=market.comparable_count,
        median_age_days=median_age,
        freshness=fresh,
        similarity_confidence=market.similarity_confidence,
        tier=tier,
        suppression=None if tier.allows_competitor_prediction else reason,
        observation_count=observation_count,
        agency_overlap_count=agency_overlap,
        activity_overlap_count=activity_overlap,
        effective_n=effective_n,
    )
