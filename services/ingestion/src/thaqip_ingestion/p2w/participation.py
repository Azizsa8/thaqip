"""Participation model (PRD FR-004): which vendors are likely to bid on a tender.

What this is
------------
An **explicitly calibrated heuristic**, not a learned model. The corpus holds
1147 vendor-attributed offers spread over 614 vendors, and 418 of those vendors
appear exactly once. Fitting even a four-feature logistic regression on that
would memorise the handful of repeat bidders and call it a model. So the
coefficients here are *chosen and documented*, every one of them lives in
:data:`PARTICIPATION_COEFFICIENTS`, and :func:`calibrate` exists to tell you —
from data, not from hope — whether they are any good.

It has been run. Against the live corpus it builds 371 real (candidate,
outcome) events over 307 historical tenders and reports Brier 0.063 with a 4.3%
observed participation rate against a 16.4% mean stated probability. Read that
plainly: **the model is still about four times over-confident in absolute
level**, even after its coefficients were cut once in response to a first
calibration run that measured 46.5% stated against the same 4.3% observed. Its
*ranking* is weakly informative (the top reliability buckets do carry a higher
observed rate than the bottom ones) but its numbers should not yet be shown to
a user as probabilities without that caveat attached.

The score
---------
For a vendor *v* and a subject tender *T*, four features are computed from
facts knowable strictly before ``as_of``:

``activity_affinity``
    Recency-weighted share of *v*'s past bids that were in *T*'s activity.
``agency_affinity``
    Recency-weighted share of *v*'s past bids that were with *T*'s agency.
``recency``
    ``0.5 ** (days_since_last_bid / HALF_LIFE_DAYS)`` — 1.0 for a bid today,
    0.5 for one a year ago.
``base_rate``
    The measured per-(vendor, tender) participation rate inside the relevant
    activity: ``offers / (distinct_vendors * distinct_tenders)``. In the largest
    activity in the corpus that is 323/(129*129) ≈ 1.9%. This is the prior, and
    it is deliberately brutal: on any given tender, a specific known bidder in
    that activity usually does *not* show up.

These are combined in **log-odds space** and mapped through a logistic, then
clamped to :data:`PROBABILITY_FLOOR`/:data:`PROBABILITY_CEILING` so the module
never states certainty about what a competitor will do (house rule 2).

Suppression
-----------
A vendor with fewer than :data:`MIN_EVIDENCE_FOR_PREDICTION` *relevant* prior
observations (bids in the same activity or with the same agency) gets
``probability=None`` and ``suppression=INSUFFICIENT_EVIDENCE``. A vendor with no
prior evidence at all is suppressed, never scored 0.5. Given the corpus, that
is the majority outcome, and it is correct output rather than missing output.

Point-in-time correctness
-------------------------
A past offer counts only once it was *public*. Bids are not visible until the
awarding results are published, so this module dates an offer by its tender's
award knowability — ``COALESCE(awards.awarded_at, awards.created_at)`` — exactly
as :mod:`thaqip_ingestion.p2w.evidence` does. ``awarded_at`` is NULL for every
row in the current corpus, so the ingestion timestamp stands in; it is provably
no earlier than the true award date, so the fallback can only *withhold*
evidence, never leak a future fact.

Honest limitations
------------------
* **The calibration sample is real but narrow.** ``awards.awarded_at`` is NULL
  corpus-wide, so award knowability is the ingestion timestamp and every award
  was ingested inside one week of September 2026. The 371 calibration events
  therefore all come from tenders whose closing date happens to fall inside or
  after that week; they are point-in-time valid (the evidence really was public
  by then) but they are one week of one crawl, not a cross-section of the
  market. A Brier score from that slice is a smoke test, not a validation.
* **The absolute level is still wrong.** 16.4% stated against 4.3% observed.
  The coefficients were cut once on this evidence and deliberately not fitted
  further — optimising four coefficients against 371 correlated events from a
  single week is how you get a number that looks validated and is not.
  :attr:`ParticipationEstimate.calibrated` is hard-coded False for that reason,
  and any UI showing these numbers must lead with the ordering, not the level.
* The reliability diagram is not monotone in the middle buckets, so even the
  ranking claim is weak: this separates "plausible bidder" from "unlikely
  bidder" and should not be read as finer-grained than that.
* Because the same ingestion artifact flattens recency (every offer looks days
  old), the ``recency`` feature is near-constant at ~1.0 today and contributes
  almost no discrimination. It is not decoration — it will bite when real award
  dates land, and the coefficients will need re-checking then.
* Candidates are drawn only from vendors with *observed* prior bids. A vendor
  who has never appeared in our corpus is invisible to this model, and its
  absence from :func:`candidate_bidders` is not evidence that it will not bid.
  With 307 of 1670 tenders carrying any offer data at all, that blind spot is
  large.
* The features are affinity and recency only. Tender size, geography, contract
  type and vendor capacity are all plausibly stronger drivers and are all absent
  — mostly because the underlying fields are sparse or login-gated.
* ``base_rate`` divides by ``vendors * tenders`` and so assumes every known
  activity bidder was eligible for every tender in that activity. That
  over-counts the denominator where activities are broad, biasing the prior low.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .contracts import (
    MODEL_VERSION,
    ExplanationFactor,
    SuppressionReason,
)

__all__ = [
    "BASE_RATE_CEILING",
    "BASE_RATE_FLOOR",
    "CALIBRATION_BUCKETS",
    "DEFAULT_BASE_RATE",
    "HALF_LIFE_DAYS",
    "MIN_BASE_RATE_TENDERS",
    "MIN_CALIBRATION_EVENTS",
    "MIN_EVIDENCE_FOR_PREDICTION",
    "PARTICIPATION_COEFFICIENTS",
    "PROBABILITY_CEILING",
    "PROBABILITY_FLOOR",
    "ParticipationEstimate",
    "ParticipationFeatures",
    "base_rate_from_rows",
    "brier_score",
    "build_features",
    "calibrate",
    "candidate_bidders",
    "estimate_from_features",
    "logistic",
    "logit",
    "normalise_history_rows",
    "participation_probability",
    "reliability_buckets",
    "resolve_as_of",
    "summarise_calibration",
]


# --------------------------------------------------------------------------
# Tunable constants.
#
# Every number below is a chosen prior with a stated reason, NOT a fitted
# value. `calibrate` is the mechanism by which they stop being guesses.
# --------------------------------------------------------------------------

#: Half-life for recency weighting, in days. One year, matching
#: :mod:`evidence`: Saudi public procurement runs on annual budget cycles, so a
#: bid from the previous cycle is worth about half a bid from the current one.
HALF_LIFE_DAYS = 365.0

#: Minimum count of *relevant* prior observations (same activity or same
#: agency) before a probability may be stated at all. Three is the smallest
#: number from which "this vendor bids in this space" is a pattern rather than a
#: coincidence; at two, one shared activity is enough to invent an affinity of
#: 1.0. 91 vendors in the corpus have exactly 2 offers, so this threshold is
#: what stops ~500 vendors being given confident-looking numbers.
MIN_EVIDENCE_FOR_PREDICTION = 3

#: Probability clamps. The floor keeps a never-seen-together vendor from being
#: reported as impossible; the ceiling enforces house rule 2 — we never imply
#: certainty about a competitor's future behaviour, however strong the history.
PROBABILITY_FLOOR = 0.01
PROBABILITY_CEILING = 0.95

#: Base-rate clamps and fallback. The measured activity participation rate can
#: degenerate (a single-tender activity gives 1.0), so it is bounded before use.
BASE_RATE_FLOOR = 0.002
BASE_RATE_CEILING = 0.5
#: Used when the activity is too thin to measure a rate from. 2% is the rate
#: measured in activity 111, the only activity in the corpus with enough tenders
#: (129) to measure one at all — so it is an observed value, not a round number.
DEFAULT_BASE_RATE = 0.02
#: Below this many distinct prior tenders in scope, the measured rate is noise
#: and DEFAULT_BASE_RATE is used instead.
MIN_BASE_RATE_TENDERS = 5

#: Log-odds coefficients. z = intercept
#:                          + base_rate      * logit(base_rate)
#:                          + activity_affinity * activity_affinity
#:                          + agency_affinity   * agency_affinity
#:                          + recency           * recency
#: Sign and magnitude rationale:
#:  * ``base_rate`` 1.0 — the prior enters at full strength and is the dominant
#:    term (logit(0.02) ≈ -3.9). Anything less would mean asserting that our
#:    four features overturn the measured market rate, which we have not shown.
#:  * ``activity_affinity`` 1.0 — the strongest signal we have: a vendor that
#:    bids exclusively in this activity gets ~2.7x the odds of one that never
#:    does. Larger than the agency term because activity describes capability,
#:    which is the harder constraint.
#:  * ``agency_affinity`` 0.7 — incumbency with the buyer matters, but Saudi
#:    public tenders are near-pure lowest-qualified-price auctions, so a
#:    relationship is a weaker participation driver than capability.
#:  * ``recency`` 0.4 — a vendor last seen a year ago is at half weight; the
#:    term modulates rather than decides.
#:  * ``intercept`` 0.0 — no unexplained offset. Any systematic bias found by
#:    :func:`calibrate` belongs here, and until a fit is actually performed it
#:    stays 0 rather than being tuned by eye.
#:
#: Magnitude check, and where these numbers came from. The features together
#: can move the log-odds by at most +2.1, i.e. an ~8x odds multiplier over the
#: market base rate. That ceiling is not arbitrary: an earlier draft used
#: 2.0/1.5/1.0 (max +4.5) and :func:`calibrate` measured it against 371 real
#: (candidate, outcome) events as grossly over-confident — mean stated
#: probability 46.5% against a 4.3% observed participation rate, Brier 0.234.
#: The revision brings the top of the range to ~20% at a 3% base rate, against
#: a measured 13% hit rate in the model's most confident bucket. These are
#: therefore *evidence-corrected priors*, not a fit: no coefficient was
#: optimised, and :attr:`ParticipationEstimate.calibrated` stays False until one
#: is. Re-run :func:`calibrate` after any change here.
PARTICIPATION_COEFFICIENTS: dict[str, float] = {
    "intercept": 0.0,
    "base_rate": 1.0,
    "activity_affinity": 1.0,
    "agency_affinity": 0.7,
    "recency": 0.4,
}

#: Calibration. Fewer than 50 (candidate, outcome) events cannot distinguish a
#: calibrated model from a broken one — with 10 buckets that is 5 per bucket
#: before any imbalance — so below it we refuse to report metrics.
MIN_CALIBRATION_EVENTS = 50
#: Reliability-diagram buckets over [0,1]. Ten is the convention and keeps each
#: bucket interpretable as a decile of stated probability.
CALIBRATION_BUCKETS = 10
#: Upper bound on historical tenders scanned per calibration run, so the call
#: stays interactive on a growing corpus. 500 > the 307 tenders that currently
#: carry any offers, so it binds nothing today.
CALIBRATION_MAX_TENDERS = 500

#: Default cap on candidates returned. 15 is a screenful in the console UI.
DEFAULT_CANDIDATE_LIMIT = 15

#: Guard for logit(): probabilities are pulled inside (eps, 1-eps) before the
#: log so a degenerate 0.0/1.0 rate cannot produce an infinity.
_LOGIT_EPS = 1e-9
#: Above this magnitude the logistic saturates to within float precision anyway;
#: clamping keeps math.exp from overflowing on a pathological coefficient set.
_LOGISTIC_CLAMP = 60.0


# --------------------------------------------------------------------------
# Basis tokens. Machine-readable, stable, and rendered by the UI — never shown
# raw, so they stay English identifiers rather than Arabic prose.
# --------------------------------------------------------------------------

BASIS_NO_EVIDENCE = "no_prior_evidence"
BASIS_INSUFFICIENT = "insufficient_relevant_evidence"
BASIS_ACTIVITY = "activity_history"
BASIS_AGENCY = "agency_history"
BASIS_ACTIVITY_AND_AGENCY = "activity_and_agency_history"


# --------------------------------------------------------------------------
# SQL. The point-in-time predicate lives here in one auditable place.
# --------------------------------------------------------------------------

#: An offer is knowable only when the awarding results were published. See the
#: module docstring for why ``created_at`` is a sound conservative fallback.
_KNOWABLE_AT = "COALESCE(a.awarded_at, a.created_at)"

#: Full bidding history of every vendor that has bid in the subject tender's
#: activity or with its agency. The vendor's *whole* history is needed, not just
#: the matching rows, because the affinities are shares and need a denominator.
#: $1 subject tender id, $2 as_of, $3 activity id, $4 agency id.
CANDIDATE_HISTORY_SQL = f"""
WITH candidate_vendors AS (
    SELECT DISTINCT o.vendor_id
    FROM offers o
    JOIN tenders t ON t.id = o.tender_id
    JOIN awards a ON a.tender_id = t.id
    WHERE o.vendor_id IS NOT NULL
      AND t.id <> $1
      AND {_KNOWABLE_AT} < $2
      AND (
            ($3::bigint IS NOT NULL AND t.activity_id = $3)
         OR ($4::bigint IS NOT NULL AND t.agency_id = $4)
      )
)
SELECT o.vendor_id,
       o.tender_id,
       t.activity_id,
       t.agency_id,
       {_KNOWABLE_AT} AS knowable_at
FROM offers o
JOIN tenders t ON t.id = o.tender_id
JOIN awards a ON a.tender_id = t.id
JOIN candidate_vendors cv ON cv.vendor_id = o.vendor_id
WHERE t.id <> $1
  AND {_KNOWABLE_AT} < $2
"""

#: One vendor's full knowable history. $1 subject tender id, $2 as_of,
#: $3 vendor id.
VENDOR_HISTORY_SQL = f"""
SELECT o.vendor_id,
       o.tender_id,
       t.activity_id,
       t.agency_id,
       {_KNOWABLE_AT} AS knowable_at
FROM offers o
JOIN tenders t ON t.id = o.tender_id
JOIN awards a ON a.tender_id = t.id
WHERE o.vendor_id = $3
  AND t.id <> $1
  AND {_KNOWABLE_AT} < $2
"""

#: Every vendor-attributed offer whose tender has an award record, with the
#: tender's own decision cutoff. Used by :func:`calibrate`, which does all its
#: slicing in memory: 1147 rows is far cheaper to fetch once than to re-query
#: per historical tender.
#:
#: The cutoff is deliberately NOT allowed to fall back to the award-knowability
#: timestamp. That timestamp is an ingestion time, so using it as a tender's own
#: decision moment would order the corpus by when the crawler happened to see
#: each row and call that history — a pseudo-time that manufactures thousands of
#: bogus calibration events out of crawl order. A tender whose real decision
#: moment is unknown cannot be placed in time, so it is skipped instead
#: (``cutoff IS NULL`` rows are dropped by :func:`calibrate`).
CALIBRATION_POOL_SQL = f"""
SELECT o.vendor_id,
       o.tender_id,
       t.activity_id,
       t.agency_id,
       {_KNOWABLE_AT} AS knowable_at,
       COALESCE(t.offers_opening_date, t.last_offer_date) AS cutoff
FROM offers o
JOIN tenders t ON t.id = o.tender_id
JOIN awards a ON a.tender_id = t.id
WHERE o.vendor_id IS NOT NULL
"""


class _Fetcher(Protocol):
    """The slice of an asyncpg connection this module uses."""

    async def fetch(self, query: str, *args: Any) -> Sequence[Any]:  # pragma: no cover
        ...


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ParticipationFeatures:
    """Point-in-time features for one vendor against one tender.

    ``relevant_count`` is the raw union of same-activity and same-agency prior
    offers and is what the suppression gate reads; the affinities are
    recency-weighted shares and are what the score reads. Both are kept because
    the UI must show the observed count as an observed fact (house rule 1) while
    the score is a derived number.
    """

    vendor_id: int
    total_count: int
    activity_count: int
    agency_count: int
    relevant_count: int
    activity_affinity: float
    agency_affinity: float
    recency: float
    days_since_last: float | None
    base_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id,
            "total_count": self.total_count,
            "activity_count": self.activity_count,
            "agency_count": self.agency_count,
            "relevant_count": self.relevant_count,
            "activity_affinity": self.activity_affinity,
            "agency_affinity": self.agency_affinity,
            "recency": self.recency,
            "days_since_last": self.days_since_last,
            "base_rate": self.base_rate,
        }


@dataclass
class ParticipationEstimate:
    """The chance that one vendor bids on one tender — or an explicit refusal.

    Exactly one of the two states holds, and it is enforced in
    ``__post_init__``:

    * ``suppression is None`` -> ``probability`` is a float in
      [PROBABILITY_FLOOR, PROBABILITY_CEILING].
    * ``suppression`` set      -> ``probability is None``.

    Mirrors the suppression invariant of :class:`~.contracts.PricePrediction`
    deliberately: a consumer that has learned to read one reads the other.
    """

    vendor_id: int
    probability: float | None
    evidence_count: int
    basis: str
    suppression: SuppressionReason | None
    factors: list[ExplanationFactor] = field(default_factory=list)
    features: ParticipationFeatures | None = None
    model_version: str = MODEL_VERSION
    calibrated: bool = False

    def __post_init__(self) -> None:
        if self.suppression is not None:
            if self.probability is not None:
                raise ValueError("a suppressed estimate must not carry a probability")
        else:
            if self.probability is None:
                raise ValueError("an unsuppressed estimate must carry a probability")
            if not 0.0 <= float(self.probability) <= 1.0:
                raise ValueError(
                    f"probability must be within [0,1], got {self.probability}"
                )
        if self.evidence_count < 0:
            raise ValueError("evidence_count must not be negative")

    @property
    def is_suppressed(self) -> bool:
        return self.suppression is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id,
            "probability": self.probability,
            "evidence_count": self.evidence_count,
            "basis": self.basis,
            "suppression": self.suppression.value if self.suppression else None,
            "is_suppressed": self.is_suppressed,
            "factors": [f.to_dict() for f in self.factors],
            "features": self.features.to_dict() if self.features else None,
            "model_version": self.model_version,
            "calibrated": self.calibrated,
        }


# --------------------------------------------------------------------------
# Pure maths. No DB, no clock — these carry the test weight.
# --------------------------------------------------------------------------

def logistic(z: float) -> float:
    """Standard logistic, saturating instead of overflowing at extreme inputs."""
    if math.isnan(z):
        raise ValueError("z must be a real number")
    clamped = max(-_LOGISTIC_CLAMP, min(_LOGISTIC_CLAMP, float(z)))
    return 1.0 / (1.0 + math.exp(-clamped))


def logit(p: float) -> float:
    """Inverse logistic. Inputs are pulled inside (0,1) so 0.0/1.0 are finite."""
    if math.isnan(p):
        raise ValueError("p must be a real number")
    bounded = max(_LOGIT_EPS, min(1.0 - _LOGIT_EPS, float(p)))
    return math.log(bounded / (1.0 - bounded))


def _coerce_utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than guessing a local zone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _age_days(as_of: datetime, moment: datetime) -> float:
    """Age in days, clamped at zero so clock skew cannot manufacture recency."""
    return max(0.0, (_coerce_utc(as_of) - _coerce_utc(moment)).total_seconds() / 86400.0)


def _decay(age_days: float, half_life_days: float = HALF_LIFE_DAYS) -> float:
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    return 0.5 ** (max(0.0, float(age_days)) / half_life_days)


def resolve_as_of(tender: dict[str, Any], as_of: datetime | None = None) -> datetime:
    """The instant we are predicting for, and the point-in-time cutoff.

    Same rule as :func:`thaqip_ingestion.p2w.evidence.resolve_as_of`, repeated
    here rather than imported so that this module has no build-order dependency
    on a sibling: explicit ``as_of`` wins, else the tender's offers-opening date
    (the moment participation is settled), else the last date offers may be
    submitted, else now.
    """
    if as_of is not None:
        return _coerce_utc(as_of)
    for key in ("offers_opening_date", "last_offer_date"):
        value = tender.get(key)
        if isinstance(value, datetime):
            return _coerce_utc(value)
    return datetime.now(UTC)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalise_history_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Coerce DB rows (asyncpg Records or dicts) into plain typed dicts.

    Rows missing a vendor id or a knowability timestamp are dropped: without
    either, the row can neither be attributed nor placed in time, and silently
    treating it as "now" would break point-in-time correctness.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        vendor_id = _as_int(row["vendor_id"])
        knowable_at = row["knowable_at"]
        if vendor_id is None or not isinstance(knowable_at, datetime):
            continue
        out.append(
            {
                "vendor_id": vendor_id,
                "tender_id": _as_int(row["tender_id"]),
                "activity_id": _as_int(row["activity_id"]),
                "agency_id": _as_int(row["agency_id"]),
                "knowable_at": _coerce_utc(knowable_at),
            }
        )
    return out


def base_rate_from_rows(
    rows: Sequence[dict[str, Any]],
    *,
    activity_id: int | None,
) -> tuple[float, int]:
    """Measured per-(vendor, tender) participation rate, and its tender count.

    ``offers / (distinct_vendors * distinct_tenders)`` over the rows scoped to
    the activity — literally "of all the chances a known bidder had to appear,
    how often did one appear". Falls back to :data:`DEFAULT_BASE_RATE` when the
    scope holds fewer than :data:`MIN_BASE_RATE_TENDERS` tenders, and the
    returned count is 0 in that case so callers can tell measured from assumed.

    Note the conditioning. :func:`candidate_bidders` passes only the histories
    of vendors already active in this activity or agency, so the rate it gets is
    "how often does a *known local* bidder appear", which is higher than the
    unconditional rate over all vendors. That is the right prior for the
    question being asked — every subject of a score is by construction such a
    vendor — but it is not a market-wide participation rate and must not be
    displayed as one.
    """
    scoped = [r for r in rows if activity_id is None or r["activity_id"] == activity_id]
    tenders = {r["tender_id"] for r in scoped if r["tender_id"] is not None}
    vendors = {r["vendor_id"] for r in scoped}
    if len(tenders) < MIN_BASE_RATE_TENDERS or not vendors:
        return DEFAULT_BASE_RATE, 0
    rate = len(scoped) / (len(tenders) * len(vendors))
    return max(BASE_RATE_FLOOR, min(BASE_RATE_CEILING, rate)), len(tenders)


def build_features(
    rows: Sequence[dict[str, Any]],
    *,
    activity_id: int | None,
    agency_id: int | None,
    as_of: datetime,
    base_rate: float,
    half_life_days: float = HALF_LIFE_DAYS,
) -> dict[int, ParticipationFeatures]:
    """Per-vendor features from already point-in-time-filtered history rows.

    Rows dated at or after ``as_of`` are dropped here as well as in SQL — the
    filter is cheap and this function is called directly by :func:`calibrate`,
    where the cutoff varies per tender and the SQL cannot express it.
    """
    per_vendor: dict[int, dict[str, float]] = {}
    for row in rows:
        if _coerce_utc(row["knowable_at"]) >= _coerce_utc(as_of):
            continue
        vendor_id = row["vendor_id"]
        age = _age_days(as_of, row["knowable_at"])
        weight = _decay(age, half_life_days)
        acc = per_vendor.setdefault(
            vendor_id,
            {
                "total": 0.0,
                "activity": 0.0,
                "agency": 0.0,
                "relevant": 0.0,
                "w_total": 0.0,
                "w_activity": 0.0,
                "w_agency": 0.0,
                "min_age": math.inf,
            },
        )
        same_activity = activity_id is not None and row["activity_id"] == activity_id
        same_agency = agency_id is not None and row["agency_id"] == agency_id
        acc["total"] += 1
        acc["w_total"] += weight
        if same_activity:
            acc["activity"] += 1
            acc["w_activity"] += weight
        if same_agency:
            acc["agency"] += 1
            acc["w_agency"] += weight
        if same_activity or same_agency:
            acc["relevant"] += 1
        acc["min_age"] = min(acc["min_age"], age)

    features: dict[int, ParticipationFeatures] = {}
    for vendor_id, acc in per_vendor.items():
        w_total = acc["w_total"]
        # A zero weighted total can only happen if every observation decayed to
        # exactly 0.0 (age past ~1000 half-lives). Treat the affinities as
        # unknown-zero rather than dividing by it.
        activity_affinity = acc["w_activity"] / w_total if w_total > 0 else 0.0
        agency_affinity = acc["w_agency"] / w_total if w_total > 0 else 0.0
        days_since_last = None if math.isinf(acc["min_age"]) else acc["min_age"]
        recency = 0.0 if days_since_last is None else _decay(days_since_last, half_life_days)
        features[vendor_id] = ParticipationFeatures(
            vendor_id=vendor_id,
            total_count=int(acc["total"]),
            activity_count=int(acc["activity"]),
            agency_count=int(acc["agency"]),
            relevant_count=int(acc["relevant"]),
            activity_affinity=activity_affinity,
            agency_affinity=agency_affinity,
            recency=recency,
            days_since_last=days_since_last,
            base_rate=base_rate,
        )
    return features


def _direction(contribution: float) -> str:
    if contribution > 0:
        return "increases"
    if contribution < 0:
        return "decreases"
    return "neutral"


def _basis_for(features: ParticipationFeatures) -> str:
    if features.activity_count and features.agency_count:
        return BASIS_ACTIVITY_AND_AGENCY
    if features.activity_count:
        return BASIS_ACTIVITY
    if features.agency_count:
        return BASIS_AGENCY
    return BASIS_NO_EVIDENCE


def estimate_from_features(
    features: ParticipationFeatures,
    *,
    coefficients: dict[str, float] | None = None,
    calibrated: bool = False,
) -> ParticipationEstimate:
    """Score one vendor. Pure: no DB, no clock, fully determined by its input.

    Suppresses (rather than guesses) below
    :data:`MIN_EVIDENCE_FOR_PREDICTION` relevant observations, and clamps every
    surviving probability into
    [:data:`PROBABILITY_FLOOR`, :data:`PROBABILITY_CEILING`].
    """
    # Merged, not replaced, so a caller can override one coefficient (a
    # sensitivity sweep, a future calibrated intercept) without restating them
    # all and silently dropping one to zero.
    coeff = {**PARTICIPATION_COEFFICIENTS, **(coefficients or {})}

    if features.relevant_count < MIN_EVIDENCE_FOR_PREDICTION:
        return ParticipationEstimate(
            vendor_id=features.vendor_id,
            probability=None,
            evidence_count=features.relevant_count,
            basis=BASIS_NO_EVIDENCE if features.relevant_count == 0 else BASIS_INSUFFICIENT,
            suppression=SuppressionReason.INSUFFICIENT_EVIDENCE,
            factors=[
                ExplanationFactor(
                    name="relevant_prior_offers",
                    direction="neutral",
                    weight=float(features.relevant_count),
                    kind="observed",
                    detail=(
                        f"{features.relevant_count} prior offers in this activity or with "
                        f"this agency; {MIN_EVIDENCE_FOR_PREDICTION} required before a "
                        "participation probability may be stated"
                    ),
                )
            ],
            features=features,
            calibrated=calibrated,
        )

    prior = coeff["base_rate"] * logit(features.base_rate)
    activity = coeff["activity_affinity"] * features.activity_affinity
    agency = coeff["agency_affinity"] * features.agency_affinity
    recency = coeff["recency"] * features.recency
    z = coeff["intercept"] + prior + activity + agency + recency
    probability = max(PROBABILITY_FLOOR, min(PROBABILITY_CEILING, logistic(z)))

    factors = [
        ExplanationFactor(
            name="relevant_prior_offers",
            direction="neutral",
            weight=float(features.relevant_count),
            kind="observed",
            detail=(
                f"{features.activity_count} prior offers in this activity, "
                f"{features.agency_count} with this agency, "
                f"{features.total_count} in total"
            ),
        ),
        ExplanationFactor(
            name="market_base_rate",
            direction=_direction(prior),
            weight=prior,
            kind="derived",
            detail=(
                f"measured participation rate for a known bidder on a given tender: "
                f"{features.base_rate:.3%}"
            ),
        ),
        ExplanationFactor(
            name="activity_affinity",
            direction=_direction(activity),
            weight=activity,
            kind="derived",
            detail=(
                f"{features.activity_affinity:.0%} of this vendor's recency-weighted "
                "bidding history is in this activity"
            ),
        ),
        ExplanationFactor(
            name="agency_affinity",
            direction=_direction(agency),
            weight=agency,
            kind="derived",
            detail=(
                f"{features.agency_affinity:.0%} of this vendor's recency-weighted "
                "bidding history is with this agency"
            ),
        ),
        ExplanationFactor(
            name="recency",
            direction=_direction(recency),
            weight=recency,
            kind="derived",
            detail=(
                "last observed bid "
                + (
                    f"{features.days_since_last:.0f} days ago"
                    if features.days_since_last is not None
                    else "not dated"
                )
            ),
        ),
    ]

    return ParticipationEstimate(
        vendor_id=features.vendor_id,
        probability=probability,
        evidence_count=features.relevant_count,
        basis=_basis_for(features),
        suppression=None,
        factors=factors,
        features=features,
        calibrated=calibrated,
    )


# --------------------------------------------------------------------------
# Calibration maths — pure, so a fixture can exercise it without a database.
# --------------------------------------------------------------------------

def brier_score(events: Sequence[tuple[float, bool]]) -> float:
    """Mean squared error of stated probabilities against binary outcomes.

    0.0 is perfect, 0.25 is what a constant 0.5 scores. Raises on an empty
    sequence: a Brier score over nothing is not 0, it is undefined, and
    returning 0.0 there would read as a perfect model.
    """
    if not events:
        raise ValueError("brier_score requires at least one event")
    return sum((float(p) - (1.0 if hit else 0.0)) ** 2 for p, hit in events) / len(events)


def reliability_buckets(
    events: Sequence[tuple[float, bool]],
    *,
    buckets: int = CALIBRATION_BUCKETS,
) -> list[dict[str, Any]]:
    """Reliability diagram: stated vs observed frequency, per probability band.

    Every bucket is returned, including empty ones, so the shape of the output
    does not depend on the data — a consumer plotting this gets a stable axis
    and an explicit "no observations here" rather than a gap it has to infer.
    """
    if buckets <= 0:
        raise ValueError("buckets must be positive")
    edges = [(i / buckets, (i + 1) / buckets) for i in range(buckets)]
    out: list[dict[str, Any]] = []
    for index, (low, high) in enumerate(edges):
        # The top bucket is closed on the right so a stated probability of
        # exactly 1.0 lands somewhere instead of falling off the end.
        is_last = index == buckets - 1
        members = [
            (p, hit)
            for p, hit in events
            if low <= p and (p <= high if is_last else p < high)
        ]
        count = len(members)
        out.append(
            {
                "lower": low,
                "upper": high,
                "count": count,
                "mean_predicted": (sum(p for p, _ in members) / count) if count else None,
                "observed_rate": (
                    sum(1 for _, hit in members if hit) / count if count else None
                ),
            }
        )
    return out


def summarise_calibration(
    events: Sequence[tuple[float, bool]],
    *,
    buckets: int = CALIBRATION_BUCKETS,
    min_events: int = MIN_CALIBRATION_EVENTS,
    tenders_scanned: int = 0,
    suppressed_candidates: int = 0,
    as_of: datetime | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Turn (probability, outcome) events into a calibration report.

    Below ``min_events`` the report is ``status='insufficient_data'`` and
    carries **no metrics at all** — not a Brier score, not buckets. A metric
    computed from 6 events is worse than no metric, because it looks like one.
    """
    base: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "coefficients": dict(PARTICIPATION_COEFFICIENTS),
        "as_of": (_coerce_utc(as_of).isoformat() if as_of else None),
        "events": len(events),
        "tenders_scanned": tenders_scanned,
        "suppressed_candidates": suppressed_candidates,
        "min_events": min_events,
    }
    if note:
        base["note"] = note
    if len(events) < min_events:
        base["status"] = "insufficient_data"
        base["suppression_reason"] = SuppressionReason.INSUFFICIENT_EVIDENCE.value
        return base
    base["status"] = "ok"
    base["brier_score"] = brier_score(events)
    base["observed_rate"] = sum(1 for _, hit in events if hit) / len(events)
    base["mean_predicted"] = sum(p for p, _ in events) / len(events)
    base["reliability_buckets"] = reliability_buckets(events, buckets=buckets)
    return base


# --------------------------------------------------------------------------
# DB-touching entry points.
# --------------------------------------------------------------------------

async def candidate_bidders(
    conn: _Fetcher,
    *,
    tender: dict[str, Any],
    as_of: datetime | None = None,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[ParticipationEstimate]:
    """Vendors plausibly bidding on ``tender``, ranked, with their estimates.

    A candidate is any vendor with at least one bid — knowable strictly before
    ``as_of`` — in the tender's activity or with its agency. Candidates whose
    relevant evidence is too thin are still returned, suppressed and ranked
    last: "we know this vendor operates here but cannot put a number on it" is
    information, and dropping them would misrepresent the field as smaller than
    it is.

    Returns at most ``limit`` estimates, scored estimates first (descending
    probability), then suppressed ones by evidence volume. Ties break on
    ``vendor_id`` so the ordering is total and the output deterministic.
    """
    if limit <= 0:
        return []
    moment = resolve_as_of(tender, as_of)
    activity_id = _as_int(tender.get("activity_id"))
    agency_id = _as_int(tender.get("agency_id"))
    if activity_id is None and agency_id is None:
        return []

    rows = normalise_history_rows(
        await conn.fetch(
            CANDIDATE_HISTORY_SQL,
            _as_int(tender.get("id")),
            moment,
            activity_id,
            agency_id,
        )
    )
    if not rows:
        return []

    base_rate, _ = base_rate_from_rows(rows, activity_id=activity_id)
    features = build_features(
        rows,
        activity_id=activity_id,
        agency_id=agency_id,
        as_of=moment,
        base_rate=base_rate,
    )
    estimates = [
        estimate_from_features(f)
        for f in features.values()
        if f.relevant_count > 0  # SQL already restricts to these; belt and braces.
    ]
    estimates.sort(
        key=lambda e: (
            0 if e.probability is not None else 1,
            -(e.probability or 0.0),
            -e.evidence_count,
            e.vendor_id,
        )
    )
    return estimates[:limit]


async def participation_probability(
    conn: _Fetcher,
    *,
    vendor_id: int,
    tender: dict[str, Any],
    as_of: datetime | None = None,
) -> ParticipationEstimate:
    """The chance ``vendor_id`` bids on ``tender``, or an explicit suppression.

    A vendor with no knowable prior offers at all comes back suppressed with
    ``evidence_count=0`` — never 0.5, never the base rate. "We have no idea" and
    "it is a coin flip" are different claims and only one of them is true here.
    """
    moment = resolve_as_of(tender, as_of)
    activity_id = _as_int(tender.get("activity_id"))
    agency_id = _as_int(tender.get("agency_id"))

    rows = normalise_history_rows(
        await conn.fetch(
            VENDOR_HISTORY_SQL,
            _as_int(tender.get("id")),
            moment,
            int(vendor_id),
        )
    )
    base_rate, _ = base_rate_from_rows(rows, activity_id=activity_id)
    features = build_features(
        rows,
        activity_id=activity_id,
        agency_id=agency_id,
        as_of=moment,
        base_rate=base_rate,
    )
    found = features.get(int(vendor_id))
    if found is None:
        found = ParticipationFeatures(
            vendor_id=int(vendor_id),
            total_count=0,
            activity_count=0,
            agency_count=0,
            relevant_count=0,
            activity_affinity=0.0,
            agency_affinity=0.0,
            recency=0.0,
            days_since_last=None,
            base_rate=base_rate,
        )
    return estimate_from_features(found)


async def calibrate(
    conn: _Fetcher,
    *,
    as_of: datetime | None = None,
    buckets: int = CALIBRATION_BUCKETS,
    min_events: int = MIN_CALIBRATION_EVENTS,
    max_tenders: int = CALIBRATION_MAX_TENDERS,
) -> dict[str, Any]:
    """Backward-looking calibration of the participation heuristic.

    For each historical tender, rebuild the candidate set from facts knowable
    strictly before that tender's own decision cutoff, score every candidate,
    and label it with whether the vendor actually bid. The result is a Brier
    score and a reliability diagram — or, below ``min_events``,
    ``status='insufficient_data'`` and nothing else.

    A tender is usable only if it carries a real decision moment
    (``offers_opening_date`` or ``last_offer_date``); see
    :data:`CALIBRATION_POOL_SQL` for why the ingestion timestamp is not allowed
    to stand in for one here.

    Reproducibility: no sampling and no randomness anywhere in this path, so a
    run over an unchanged corpus is bit-for-bit repeatable given the same
    ``as_of``.

    Against the live corpus this currently reports ``status='ok'`` on 371
    events with Brier 0.063, 4.3% observed against 16.4% stated. That is a
    smoke test on one week of crawl data, not a validation — see the module
    docstring before quoting either number.
    """
    moment = _coerce_utc(as_of) if as_of is not None else datetime.now(UTC)
    raw = await conn.fetch(CALIBRATION_POOL_SQL)

    pool = normalise_history_rows(raw)
    # Cutoffs live on the raw rows, keyed by tender; a tender with no usable
    # cutoff cannot be placed in time and is skipped rather than assumed.
    cutoffs: dict[int, datetime] = {}
    tender_meta: dict[int, tuple[int | None, int | None]] = {}
    actual_bidders: dict[int, set[int]] = {}
    for row in raw:
        tender_id = _as_int(row["tender_id"])
        vendor_id = _as_int(row["vendor_id"])
        cutoff = row["cutoff"]
        if tender_id is None or vendor_id is None or not isinstance(cutoff, datetime):
            continue
        cutoffs[tender_id] = _coerce_utc(cutoff)
        tender_meta[tender_id] = (_as_int(row["activity_id"]), _as_int(row["agency_id"]))
        actual_bidders.setdefault(tender_id, set()).add(vendor_id)

    # Oldest first, so a truncated run calibrates on a contiguous early slice
    # rather than an arbitrary one.
    ordered = sorted(cutoffs.items(), key=lambda kv: (kv[1], kv[0]))[:max_tenders]

    events: list[tuple[float, bool]] = []
    suppressed = 0
    scanned = 0
    for tender_id, cutoff in ordered:
        if cutoff >= moment:
            continue  # not yet decided as of the evaluation instant
        scanned += 1
        activity_id, agency_id = tender_meta[tender_id]
        if activity_id is None and agency_id is None:
            continue
        history = [
            r for r in pool if r["tender_id"] != tender_id and r["knowable_at"] < cutoff
        ]
        if not history:
            continue
        base_rate, _ = base_rate_from_rows(history, activity_id=activity_id)
        features = build_features(
            history,
            activity_id=activity_id,
            agency_id=agency_id,
            as_of=cutoff,
            base_rate=base_rate,
        )
        bidders = actual_bidders.get(tender_id, set())
        for vendor_id, feat in features.items():
            if feat.relevant_count <= 0:
                continue  # not a candidate for this tender at all
            estimate = estimate_from_features(feat)
            if estimate.probability is None:
                suppressed += 1
                continue
            events.append((estimate.probability, vendor_id in bidders))

    note = None
    if not events:
        note = (
            "no (candidate, outcome) events could be constructed: no vendor bidding "
            "history was knowable before any historical tender's cutoff under the "
            "point-in-time rule"
        )
    return summarise_calibration(
        events,
        buckets=buckets,
        min_events=min_events,
        tenders_scanned=scanned,
        suppressed_candidates=suppressed,
        as_of=moment,
        note=note,
    )
