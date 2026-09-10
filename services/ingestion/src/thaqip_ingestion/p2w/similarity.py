"""Similar-tender retrieval (PRD FR-003).

WHAT THIS IS
------------
A transparent, weighted, *rule-based* nearest-neighbour retrieval over the
tenders table.  Given a subject tender it returns comparable historical
tenders together with the per-component scores that produced the ranking and,
crucially, the reason a candidate was *not* comparable.  Both the market
baseline (which averages comparables) and the UI ("why wasn't X comparable?")
read this output, so every number here is either an observed fact copied from
the database or a derived score computed from observed facts.  Nothing in this
module predicts anything.

WHY RULES AND NOT EMBEDDINGS
----------------------------
The corpus is ~1.7k tenders with ~300 awards.  A learned similarity model
would be fit on a few hundred points and would be impossible to explain to a
bidder who is about to commit money.  A weighted sum of six named components
is auditable, deterministic, and degrades honestly: when a component has no
input we say so (a neutral score plus a note) instead of inventing one.

POINT-IN-TIME CORRECTNESS (hard gate)
-------------------------------------
A candidate is eligible only if its *point-in-time timestamp* is strictly
before ``as_of``.  That timestamp is the first available of
``offers_opening_date`` -> ``last_offer_date`` -> ``published_at``.  Awards in
this database carry ``award_value`` but ``awarded_at`` is NULL for every row
(measured 2026-09), so the opening date is the best available proxy for "the
moment this tender's price became knowable".  A candidate with *no* usable
timestamp cannot be proven to be in the past and is therefore excluded as
``future_relative_to_as_of`` — conservative by design.

Post-award facts (winner identity, final bidder count, award value ranking)
are never similarity *inputs*.  ``award_value`` is used only as a scale proxy,
and only for candidates that are already proven to be in the past; the subject
tender's own award value is never required.  ``bidder_count`` is carried on the
result for display and is deliberately not a scored component.

HONEST LIMITATIONS
------------------
* Region is inferred from free text (agency + branch name) against a fixed
  gazetteer of the 13 Saudi administrative regions and their major cities.  A
  tender whose agency text names no region scores the neutral 0.5, not 0.
* Duration similarity uses the *tendering window* (publication -> last offer
  date), which is a procurement-calendar artefact, not contract duration.
  Contract duration is not present in the Etimad payload we ingest.  Where
  either endpoint is missing the component is neutral with a note.
* Semantic similarity is token-overlap Jaccard on normalized Arabic names.  It
  has no synonym knowledge: "صيانة" and "تشغيل وصيانة" overlap, "شراء" and
  "توريد" do not.  Tender documents are login-gated so richer text is absent.
* Scale similarity falls back from award value to booklet price, and booklet
  price is 0.00 for a large share of rows, in which case scale is neutral.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..resolution import normalize_ar
from .contracts import MODEL_VERSION

__all__ = [
    "RETRIEVAL_VERSION",
    "SIMILARITY_WEIGHTS",
    "SimilarTender",
    "activity_match",
    "agency_match",
    "duration_similarity",
    "find_similar_tenders",
    "normalized_tokens",
    "persist_similarity",
    "point_in_time",
    "region_match",
    "region_of",
    "scale_similarity",
    "semantic_similarity",
    "token_jaccard",
]

# Retrieval algorithm version, persisted with every row so a later change is
# additive rather than destructive.  Tied to the model version for traceability
# but versioned separately: retrieval may change without the model changing.
RETRIEVAL_VERSION = "sim-v1"

# Component weights.  Rationale (Saudi public tenders are near-pure
# lowest-qualified-price auctions, so *what* and *how big* dominate *who*):
#   activity 0.30 - the single strongest determinant of price level.
#   scale    0.20 - a 10x bigger job is not a comparable, whatever else matches.
#   semantic 0.20 - the tender name is the only free text we reliably have.
#   agency   0.15 - buying behaviour and evaluation style cluster by agency.
#   region   0.10 - labour/logistics cost varies by region, second order.
#   duration 0.05 - weakest signal here; it is a calendar artefact (see docstring).
SIMILARITY_WEIGHTS: dict[str, float] = {
    "activity": 0.30,
    "agency": 0.15,
    "scale": 0.20,
    "region": 0.10,
    "duration": 0.05,
    "semantic": 0.20,
}

# --- named constants (no magic numbers below this line) ---------------------

# A candidate scoring below this on scale is a different size of job entirely;
# 0.15 on the log10 curve corresponds to roughly a 7x price gap.
SCALE_INCOMPARABLE_THRESHOLD = 0.15
# One order of magnitude (10x) is the point where scale similarity reaches 0.
SCALE_LOG_BASE = 10.0
# Score used when a component has no usable input on either side.  0.5 says
# "unknown", which is deliberately different from 0.0 ("known to differ").
NEUTRAL_SCORE = 0.5
# Partial credit when activity ids differ but the activity *names* overlap
# strongly enough to look like the same family of work.
ACTIVITY_RELATED_SCORE = 0.5
# Jaccard over normalized activity-name tokens at or above this counts as
# "same family of work".  Chosen so that a shared head noun plus one shared
# qualifier out of ~4 tokens clears the bar, while a single shared stopword
# does not.
ACTIVITY_RELATED_JACCARD = 0.34
# Tendering-window difference (days) at which duration similarity reaches 0.
# Etimad windows cluster between ~7 and ~60 days, so 60 spans the real range.
DURATION_SPAN_DAYS = 60.0
# Hard cap on rows pulled from the database for scoring, so retrieval stays
# O(1) in database work as the corpus grows.  The corpus is ~1.7k rows today.
CANDIDATE_POOL_LIMIT = 5000
# Tokens too generic to carry meaning in a tender name; dropped before Jaccard.
_STOPWORDS_RAW = (
    "منافسة", "مشروع", "عقد", "كراسة", "رقم", "على", "في", "من", "الى", "إلى",
    "عدة", "لزوم", "تابعة", "و", "مع", "بمنطقة", "لدى",
)
_STOPWORDS: frozenset[str] = frozenset(normalize_ar(w) for w in _STOPWORDS_RAW)

# Saudi administrative regions and the major cities that stand in for them in
# agency/branch free text.  Longest alias first at match time so that
# "المدينة المنورة" is not shadowed by a shorter alias.
_REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "riyadh": ("الرياض", "الخرج", "الدرعية"),
    "makkah": ("مكة المكرمة", "مكة", "جدة", "الطائف", "رابغ", "القنفذة"),
    "madinah": ("المدينة المنورة", "المدينة", "ينبع"),
    "qassim": ("القصيم", "بريدة", "عنيزة"),
    "eastern": ("المنطقة الشرقية", "الشرقية", "الدمام", "الظهران", "الخبر",
                "الأحساء", "الهفوف", "الجبيل", "القطيف", "حفر الباطن"),
    "asir": ("عسير", "أبها", "خميس مشيط", "بيشة"),
    "tabuk": ("تبوك", "ضباء"),
    "hail": ("حائل",),
    "northern_borders": ("الحدود الشمالية", "عرعر", "رفحاء"),
    "jazan": ("جازان", "جيزان", "صبيا"),
    "najran": ("نجران",),
    "bahah": ("الباحة", "بلجرشي"),
    "jouf": ("الجوف", "سكاكا", "القريات"),
}
# Pre-normalized (alias, region) pairs, longest alias first.
_REGION_INDEX: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((normalize_ar(alias), region)
         for region, aliases in _REGION_ALIASES.items()
         for alias in aliases),
        key=lambda pair: -len(pair[0]),
    )
)

# Machine-readable exclusion reasons.  These are *retrieval* exclusions, not
# prediction suppressions (those live in contracts.SuppressionReason); a caller
# that ends up with zero comparables should raise a SuppressionReason of its own.
EXCLUSION_SELF = "self"
EXCLUSION_FUTURE = "future_relative_to_as_of"
EXCLUSION_NO_AWARD_VALUE = "no_award_value"
EXCLUSION_SCALE = "scale_incomparable"
EXCLUSION_REASONS: tuple[str, ...] = (
    EXCLUSION_SELF,
    EXCLUSION_FUTURE,
    EXCLUSION_NO_AWARD_VALUE,
    EXCLUSION_SCALE,
)

_SECONDS_PER_DAY = 86400.0


@dataclass
class SimilarTender:
    """One scored candidate.  Every field is observed or derived, never predicted."""

    tender_id: int
    name: str
    agency: str | None
    award_value: float | None
    bidder_count: int
    total_score: float
    components: dict[str, float]
    exclusion_reason: str | None
    age_days: float
    notes: tuple[str, ...] = ()

    @property
    def is_excluded(self) -> bool:
        return self.exclusion_reason is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tender_id": self.tender_id,
            "name": self.name,
            "agency": self.agency,
            "award_value": self.award_value,
            "bidder_count": self.bidder_count,
            "total_score": self.total_score,
            "components": dict(self.components),
            "exclusion_reason": self.exclusion_reason,
            "age_days": self.age_days,
            "notes": list(self.notes),
            "retrieval_version": RETRIEVAL_VERSION,
            "model_version": MODEL_VERSION,
        }


# --------------------------------------------------------------------------
# pure scoring helpers (no database, no clock)
# --------------------------------------------------------------------------


def normalized_tokens(text: str | None) -> frozenset[str]:
    """Normalized, stopword-filtered token set of an Arabic tender/activity name.

    Normalization is delegated to ``resolution.normalize_ar`` (strips diacritics
    and tatweel, unifies hamza forms to alef, taa marbuta to haa, alef maqsura
    to yaa) so retrieval and entity resolution can never drift apart.
    """
    if not text:
        return frozenset()
    normalized = normalize_ar(text)
    tokens = {
        "".join(ch for ch in raw if ch.isalnum())
        for raw in normalized.split()
    }
    return frozenset(t for t in tokens if t and t not in _STOPWORDS)


def token_jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard overlap of two token sets; 0.0 when either side is empty."""
    sa, sb = frozenset(a), frozenset(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def activity_match(
    a_id: int | None,
    b_id: int | None,
    a_name: str | None = None,
    b_name: str | None = None,
) -> float:
    """1.0 exact activity id, ACTIVITY_RELATED_SCORE for a related name, else 0."""
    if a_id is not None and b_id is not None and int(a_id) == int(b_id):
        return 1.0
    overlap = token_jaccard(normalized_tokens(a_name), normalized_tokens(b_name))
    if overlap >= ACTIVITY_RELATED_JACCARD:
        return ACTIVITY_RELATED_SCORE
    return 0.0


def agency_match(a_id: int | None, b_id: int | None) -> float:
    """1.0 for the same resolved agency id, else 0.  Ids only: raw agency text is
    unreliable enough that ``resolution`` exists precisely to canonicalize it."""
    if a_id is None or b_id is None:
        return 0.0
    return 1.0 if int(a_id) == int(b_id) else 0.0


def scale_similarity(a: float | None, b: float | None) -> float | None:
    """Symmetric log-ratio scale similarity, or None when it cannot be computed.

    ``1 - min(1, |log10(a/b)| / log10(10))`` so equal values score 1.0, a 10x
    gap scores 0.0, and the function is symmetric in its arguments by
    construction (|log(a/b)| == |log(b/a)|).  Non-positive values carry no
    ratio information and yield None (the caller substitutes NEUTRAL_SCORE and
    records a note) rather than a fabricated score.
    """
    if a is None or b is None:
        return None
    fa, fb = float(a), float(b)
    if fa <= 0.0 or fb <= 0.0:
        return None
    gap = abs(math.log(fa / fb) / math.log(SCALE_LOG_BASE))
    return 1.0 - min(1.0, gap)


def region_of(*texts: str | None) -> str | None:
    """Best-effort region key from agency / branch free text.

    Heuristic: normalize the concatenated text, then look for the longest
    matching gazetteer alias as a substring.  Substring (not token) matching is
    used because the real data writes region names glued to other words, e.g.
    "بمنطقة المدينةالمنورة".  Returns None when no alias is found — an honest
    "unknown", scored neutrally rather than as a mismatch.
    """
    blob = normalize_ar(" ".join(t for t in texts if t))
    if not blob:
        return None
    compact = blob.replace(" ", "")
    for alias, region in _REGION_INDEX:
        if alias in blob or alias.replace(" ", "") in compact:
            return region
    return None


def region_match(a_region: str | None, b_region: str | None) -> float:
    """1.0 same region, 0.0 different regions, NEUTRAL_SCORE if either unknown."""
    if a_region is None or b_region is None:
        return NEUTRAL_SCORE
    return 1.0 if a_region == b_region else 0.0


def _window_days(published: datetime | None, last_offer: datetime | None) -> float | None:
    """Tendering window in days (publication -> last offer date), or None."""
    if published is None or last_offer is None:
        return None
    delta = (_aware(last_offer) - _aware(published)).total_seconds() / _SECONDS_PER_DAY
    return delta if delta >= 0.0 else None


def duration_similarity(a_days: float | None, b_days: float | None) -> float | None:
    """Linear closeness of two tendering windows; None when either is missing."""
    if a_days is None or b_days is None:
        return None
    return 1.0 - min(1.0, abs(a_days - b_days) / DURATION_SPAN_DAYS)


def semantic_similarity(a_name: str | None, b_name: str | None) -> float:
    """Token-overlap Jaccard on normalized Arabic tender names."""
    return token_jaccard(normalized_tokens(a_name), normalized_tokens(b_name))


def _aware(value: datetime) -> datetime:
    """Treat naive datetimes as UTC so fixtures and asyncpg rows compare safely."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def point_in_time(row: Mapping[str, Any]) -> datetime | None:
    """The moment a tender's price became knowable.

    First available of offers_opening_date -> last_offer_date -> published_at.
    ``awards.awarded_at`` is deliberately NOT consulted: it is NULL for every
    award row in this database, so relying on it would silently disable the
    point-in-time gate.
    """
    for key in ("offers_opening_date", "last_offer_date", "published_at"):
        value = row.get(key)
        if isinstance(value, datetime):
            return _aware(value)
    return None


def _weighted_total(components: Mapping[str, float]) -> float:
    return sum(SIMILARITY_WEIGHTS[k] * float(components[k]) for k in SIMILARITY_WEIGHTS)


def _scale_pair(
    subject: Mapping[str, Any], candidate: Mapping[str, Any]
) -> tuple[float | None, float | None, str]:
    """Pick the comparable magnitude for both sides: award value, else booklet price."""
    s_award, c_award = subject.get("award_value"), candidate.get("award_value")
    if s_award is not None and c_award is not None:
        return float(s_award), float(c_award), "award_value"
    s_book, c_book = subject.get("booklet_price"), candidate.get("booklet_price")
    if s_book is not None and c_book is not None:
        return float(s_book), float(c_book), "booklet_price"
    return None, None, "unavailable"


def score_candidate(
    subject: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    as_of: datetime,
) -> SimilarTender:
    """Score one candidate against the subject.  Pure: no database, no clock.

    Both mappings use tenders-table column names.  The exclusion reason is set
    but the row is still returned with its components, because the UI has to be
    able to answer "why wasn't X comparable?".
    """
    notes: list[str] = []

    subject_region = region_of(subject.get("agency_name_raw"), subject.get("branch_name"))
    candidate_region = region_of(candidate.get("agency_name_raw"), candidate.get("branch_name"))

    s_scale, c_scale, scale_basis = _scale_pair(subject, candidate)
    raw_scale = scale_similarity(s_scale, c_scale)
    if raw_scale is None:
        notes.append(f"scale_neutral:{scale_basis}")

    a_days = _window_days(subject.get("published_at"), subject.get("last_offer_date"))
    b_days = _window_days(candidate.get("published_at"), candidate.get("last_offer_date"))
    raw_duration = duration_similarity(a_days, b_days)
    if raw_duration is None:
        notes.append("duration_neutral:missing_dates")
    if subject_region is None or candidate_region is None:
        notes.append("region_neutral:unknown_region")

    components: dict[str, float] = {
        "activity": activity_match(
            subject.get("activity_id"),
            candidate.get("activity_id"),
            subject.get("activity_name_raw"),
            candidate.get("activity_name_raw"),
        ),
        "agency": agency_match(subject.get("agency_id"), candidate.get("agency_id")),
        "scale": NEUTRAL_SCORE if raw_scale is None else raw_scale,
        "region": region_match(subject_region, candidate_region),
        "duration": NEUTRAL_SCORE if raw_duration is None else raw_duration,
        "semantic": semantic_similarity(subject.get("name"), candidate.get("name")),
    }

    candidate_pit = point_in_time(candidate)
    age_days = (
        (_aware(as_of) - candidate_pit).total_seconds() / _SECONDS_PER_DAY
        if candidate_pit is not None
        else float("nan")
    )

    # Exclusion precedence: identity, then the point-in-time gate (a leak is the
    # most serious failure), then missing outcome, then incomparable size.
    exclusion: str | None = None
    if subject.get("id") is not None and candidate.get("id") == subject.get("id"):
        exclusion = EXCLUSION_SELF
    elif candidate_pit is None:
        notes.append("no_point_in_time_timestamp")
        exclusion = EXCLUSION_FUTURE
    elif candidate_pit >= _aware(as_of):
        exclusion = EXCLUSION_FUTURE
    elif candidate.get("award_value") is None:
        exclusion = EXCLUSION_NO_AWARD_VALUE
    elif raw_scale is not None and raw_scale < SCALE_INCOMPARABLE_THRESHOLD:
        exclusion = EXCLUSION_SCALE

    return SimilarTender(
        tender_id=int(candidate["id"]),
        name=str(candidate.get("name") or ""),
        agency=candidate.get("agency_name_raw"),
        award_value=(
            float(candidate["award_value"]) if candidate.get("award_value") is not None else None
        ),
        bidder_count=int(candidate.get("bidder_count") or 0),
        total_score=round(_weighted_total(components), 6),
        components={k: round(float(v), 6) for k, v in components.items()},
        exclusion_reason=exclusion,
        age_days=round(age_days, 3) if not math.isnan(age_days) else age_days,
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------
# database-touching entry points
# --------------------------------------------------------------------------

_CANDIDATE_SQL = """
SELECT t.id, t.name, t.agency_id, t.agency_name_raw, t.branch_name,
       t.activity_id, t.activity_name_raw, t.booklet_price,
       t.published_at, t.last_offer_date, t.offers_opening_date,
       a.award_value,
       COALESCE(o.bidder_count, 0) AS bidder_count
FROM tenders t
LEFT JOIN LATERAL (
    SELECT aw.award_value FROM awards aw
    WHERE aw.tender_id = t.id AND aw.award_value IS NOT NULL
    ORDER BY aw.id LIMIT 1
) a ON TRUE
LEFT JOIN LATERAL (
    SELECT count(*)::int AS bidder_count FROM offers of WHERE of.tender_id = t.id
) o ON TRUE
WHERE t.id <> $1
  AND (t.activity_id = $2 OR t.agency_id = $3 OR a.award_value IS NOT NULL)
ORDER BY t.id
LIMIT $4
"""


async def find_similar_tenders(
    conn: Any,
    *,
    tender: Mapping[str, Any],
    as_of: datetime | None = None,
    limit: int = 20,
    include_excluded: bool = False,
) -> list[SimilarTender]:
    """Return comparable tenders for ``tender``, best first.

    ``tender`` is a mapping with tenders-table column names (at minimum ``id``
    and ``name``).  ``as_of`` defaults to the subject tender's own point-in-time
    timestamp, falling back to now: the caller building features for a *past*
    tender therefore gets the point-in-time gate for free instead of having to
    remember it.

    With ``include_excluded=True`` the excluded candidates are appended after
    the eligible ones (each capped at ``limit``) so the UI can show why a
    particular tender was not used.
    """
    effective_as_of = _aware(as_of) if as_of is not None else point_in_time(tender)
    if effective_as_of is None:
        effective_as_of = datetime.now(UTC)

    rows = await conn.fetch(
        _CANDIDATE_SQL,
        int(tender["id"]),
        tender.get("activity_id"),
        tender.get("agency_id"),
        CANDIDATE_POOL_LIMIT,
    )
    scored = [score_candidate(tender, dict(row), as_of=effective_as_of) for row in rows]

    # Deterministic ordering: score desc, then tender_id asc to break ties.
    def _rank(item: SimilarTender) -> tuple[float, int]:
        return (-item.total_score, item.tender_id)

    eligible = sorted((s for s in scored if not s.is_excluded), key=_rank)[:limit]
    if not include_excluded:
        return eligible
    excluded = sorted((s for s in scored if s.is_excluded), key=_rank)[:limit]
    return eligible + excluded


_PERSIST_SQL = """
INSERT INTO tender_similarity
    (tender_id, similar_tender_id, total_score, components, exclusion_reason,
     retrieval_version, computed_at)
VALUES ($1, $2, $3, $4::jsonb, $5, $6, now())
ON CONFLICT (tender_id, similar_tender_id, retrieval_version) DO UPDATE
SET total_score = EXCLUDED.total_score,
    components = EXCLUDED.components,
    exclusion_reason = EXCLUDED.exclusion_reason,
    computed_at = now()
"""


async def persist_similarity(
    conn: Any,
    tender_id: int,
    results: Sequence[SimilarTender],
    retrieval_version: str = RETRIEVAL_VERSION,
) -> int:
    """Upsert scored candidates into ``tender_similarity``; returns the row count.

    Self-rows are never written (a tender is not its own comparable).  Excluded
    rows ARE written, with their reason, because the exclusion is part of the
    evidence trail.
    """
    import json

    payload = [
        (
            int(tender_id),
            r.tender_id,
            float(r.total_score),
            json.dumps(r.components, ensure_ascii=False),
            r.exclusion_reason,
            retrieval_version,
        )
        for r in results
        if r.tender_id != int(tender_id)
    ]
    if not payload:
        return 0
    await conn.executemany(_PERSIST_SQL, payload)
    return len(payload)


# Guard the weight table at import time: a future edit that forgets to
# rebalance must fail loudly, not silently rescale every score.
_WEIGHT_SUM_TOLERANCE = 1e-9
if abs(sum(SIMILARITY_WEIGHTS.values()) - 1.0) > _WEIGHT_SUM_TOLERANCE:  # pragma: no cover
    raise RuntimeError("SIMILARITY_WEIGHTS must sum to 1.0")

