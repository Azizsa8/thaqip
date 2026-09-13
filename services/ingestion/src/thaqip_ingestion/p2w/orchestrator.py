"""Composition root: the single P2W entry point the console API calls.

Everything else in ``p2w`` is a model that does one thing and can be tested in
isolation. This module is the only place that knows the *order* they run in, how
one's suppression constrains the next, and what the whole thing is allowed to
say when a piece of it is missing.

Two guarantees the callers depend on:

**Graceful degradation.** No sub-model failure reaches the API. Every stage runs
inside a guard; a failure is logged, recorded in ``degradations`` with a machine
readable ``SuppressionReason``, and the capability level drops to whatever the
surviving stages actually support. The alternative — a 500 — turns a thin-data
tender into an outage, and thin data is the normal case in this market.

**The capability ladder.** ``allowed_level`` is the contract between the engine
and the UI: it says what the page is permitted to render, so the decision is
made once, here, against the evidence, rather than re-derived in a template.

    L0  facts only. No supportable market range (tier D, or the market model
        failed). The page shows the tender and the comparables and no price.
    L1  market range. A market P10/P50/P90 exists. Competitor prices are silent.
    L2  named field. L1 plus at least one candidate bidder with a supportable
        participation probability — who is likely to show up, not what they bid.
    L3  competitor bands. L2 plus at least one competitor price band, which
        requires tier B or better for that vendor (tier B bands are widened).
    L4  simulation grade. L3 with at least one tier-A competitor band, the only
        state in which a Monte Carlo field is built from strong evidence rather
        than from one widened band.

The ladder is monotone: a level implies every level below it. It is defined here
because the readiness guide it comes from is not in this repository; the
definitions above are the authoritative version for this code.

**Point-in-time.** One ``as_of`` is resolved at the top and passed to every
sub-model. They each have their own fallback, and letting them each resolve their
own would let the evidence grade and the sample disagree about which facts were
knowable — the one failure mode that silently invalidates every number.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .contracts import (
    MODEL_VERSION,
    EvidenceTier,
    ExplanationFactor,
    PredictionScope,
    PricePrediction,
    SuppressionReason,
)

log = logging.getLogger(__name__)

LEVEL_L0 = "L0"
LEVEL_L1 = "L1"
LEVEL_L2 = "L2"
LEVEL_L3 = "L3"
LEVEL_L4 = "L4"
#: Ascending, so ``LEVELS.index`` orders them and ``min`` caps them.
LEVELS: tuple[str, ...] = (LEVEL_L0, LEVEL_L1, LEVEL_L2, LEVEL_L3, LEVEL_L4)

#: How many candidate bidders we price. Each one costs several queries, and a
#: field larger than this is not a field the user can reason about anyway.
DEFAULT_COMPETITOR_LIMIT = 8

#: Scenario price grid: fractions of the market P10 and P90, and the point count.
GRID_LOW_FACTOR = 0.6
GRID_HIGH_FACTOR = 1.4
GRID_POINTS = 40

#: Assumed probability that a bid clears technical evaluation. There is no
#: technical_pass data in this database (``offers.technical_pass`` is unpopulated),
#: so this is an *assumption*, not a measurement. It is surfaced verbatim in the
#: scenario's ``assumptions`` block so a reader can see it is not evidence.
DEFAULT_TECHNICAL_PASS_P = 0.95

#: Stages named in ``degradations`` entries.
STAGE_TENDER = "tender"
STAGE_SIMILARITY = "similarity"
STAGE_MARKET = "market"
STAGE_PARTICIPATION = "participation"
STAGE_COMPETITOR = "competitor"
STAGE_EXPLANATION = "explanation"
STAGE_SIMULATION = "simulation"
STAGE_OPTIMIZER = "optimizer"
STAGE_PERSISTENCE = "persistence"

_TENDER_SQL = """
SELECT t.id, t.source, t.reference_number, t.name, t.agency_id, t.agency_name_raw,
       t.branch_name, t.activity_id, t.activity_name_raw, t.tender_type_id,
       t.tender_type_name, t.status_id, t.status_name, t.booklet_price,
       t.published_at, t.last_enquiries_date, t.last_offer_date, t.offers_opening_date,
       t.submitted_bids_count, t.draft_bids_count, t.external_bids_count,
       ag.canonical_name AS agency_canonical_name
FROM tenders t
LEFT JOIN agencies ag ON ag.id = t.agency_id
WHERE t.id = $1
"""

_VENDOR_NAME_SQL = "SELECT id, canonical_name FROM vendors WHERE id = ANY($1::bigint[])"

_INSERT_PREDICTION_SQL = """
INSERT INTO price_predictions (
    tender_id, prediction_scope, subject_vendor_id, p10, p50, p90, expected_value,
    win_probability, confidence_score, similarity_confidence, data_freshness_score,
    evidence_count, evidence_tier, model_version, feature_snapshot_id, seed,
    suppression_reason, explanation_factors, generated_at, origin, snapshot_date
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
    $18::jsonb, $19, $20, $21
)
RETURNING id
"""

_LATEST_SCENARIO_SQL = """
SELECT id, version
FROM user_bid_scenarios
WHERE tenant_id = $1 AND tender_id = $2
ORDER BY version DESC, id DESC
LIMIT 1
"""

_INSERT_SCENARIO_SQL = """
INSERT INTO user_bid_scenarios (
    tenant_id, tender_id, name, estimated_cost, min_margin_pct, target_win_pct,
    proposed_bid, risk_reserve, version, seed, created_by
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
RETURNING id, version, created_at
"""

_SUPERSEDE_SCENARIO_SQL = """
UPDATE user_bid_scenarios SET superseded_by = $2 WHERE id = $1
"""


class TenderNotFound(LookupError):
    """The requested tender id does not exist.

    Raised, unlike every sub-model failure: this is the caller asking about
    something that is not there, not a model that could not answer. Degrading it
    into an empty intelligence payload would hide a bad id behind a plausible
    looking page.
    """


@dataclass
class _Degradation:
    """One stage that did not deliver, and what that costs the page."""

    stage: str
    reason: SuppressionReason
    detail: str
    caps_level: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "reason": self.reason.value,
            "detail": self.detail,
            "caps_level": self.caps_level,
        }


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _cap(level: str, ceiling: str) -> str:
    """The lower of two ladder levels."""
    return LEVELS[min(LEVELS.index(level), LEVELS.index(ceiling))]


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _numeric(value: Any) -> Decimal | None:
    """Float -> Decimal for asyncpg's ``numeric`` codec, which refuses floats."""
    number = _as_float(value)
    if number is None:
        return None
    return Decimal(str(number))


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _tender_summary(tender: Mapping[str, Any], as_of: datetime) -> dict[str, Any]:
    """The observed tender facts the page needs. All of it is on record."""
    return {
        "id": _as_int(tender.get("id")),
        "name": tender.get("name"),
        "reference_number": tender.get("reference_number"),
        "source": tender.get("source"),
        "agency_id": _as_int(tender.get("agency_id")),
        "agency_name": tender.get("agency_canonical_name") or tender.get("agency_name_raw"),
        "branch_name": tender.get("branch_name"),
        "activity_id": _as_int(tender.get("activity_id")),
        "activity_name": tender.get("activity_name_raw"),
        "tender_type_name": tender.get("tender_type_name"),
        "status_name": tender.get("status_name"),
        "booklet_price": _as_float(tender.get("booklet_price")),
        "published_at": _iso(tender.get("published_at")),
        "last_enquiries_date": _iso(tender.get("last_enquiries_date")),
        "last_offer_date": _iso(tender.get("last_offer_date")),
        "offers_opening_date": _iso(tender.get("offers_opening_date")),
        "submitted_bids_count": _as_int(tender.get("submitted_bids_count")),
        "as_of": as_of.isoformat(),
        "kind": "observed",
    }


async def load_tender(conn: Any, tender_id: int) -> dict[str, Any]:
    """Fetch the tender row, or raise :class:`TenderNotFound`."""
    row = await conn.fetchrow(_TENDER_SQL, int(tender_id))
    if row is None:
        raise TenderNotFound(f"tender {tender_id} does not exist")
    return dict(row)


def resolve_as_of(tender: Mapping[str, Any], as_of: datetime | None = None) -> datetime:
    """The one point-in-time cutoff for this whole computation.

    Delegates to ``market._resolve_cutoff``, which takes the *earlier* of the
    evidence and similarity horizons — the stricter of the two, because a leak is
    worse than an over-narrow sample.
    """
    from . import market as market_mod

    return market_mod._resolve_cutoff(tender, as_of)


async def _vendor_names(conn: Any, vendor_ids: Sequence[int]) -> dict[int, str]:
    ids = sorted({_as_int(v) for v in vendor_ids} - {None})
    if not ids:
        return {}
    try:
        rows = await conn.fetch(_VENDOR_NAME_SQL, ids)
    except Exception as exc:  # noqa: BLE001 - a name lookup may never break the page
        log.warning("vendor name lookup failed: %s", exc)
        return {}
    return {
        _as_int(dict(r).get("id")): str(dict(r).get("canonical_name") or "")
        for r in rows or []
        if _as_int(dict(r).get("id")) is not None
    }


# --------------------------------------------------------------------------
# tender intelligence
# --------------------------------------------------------------------------


async def tender_intelligence(
    conn: Any,
    *,
    tender_id: int,
    as_of: datetime | None = None,
    tenant_id: int = 1,
    include_competitors: bool = True,
) -> dict[str, Any]:
    """Everything the engine can honestly say about one tender, in one dict.

    Order: tender -> similarity -> market -> evidence tier -> (if the tier
    permits) candidate bidders -> per-candidate competitor bands -> explanation.
    Each step is guarded; a failure caps ``allowed_level`` and appends to
    ``degradations`` instead of propagating.

    ``tenant_id`` is carried through to the response but no tenant-private data
    is read here: this payload is built entirely from shared market record, which
    is what makes it cacheable across tenants.
    """
    from . import competitor as competitor_mod
    from . import explain as explain_mod
    from . import market as market_mod
    from . import participation as participation_mod
    from . import similarity as similarity_mod

    tender = await load_tender(conn, tender_id)
    cutoff = resolve_as_of(tender, as_of)
    degradations: list[_Degradation] = []
    level = LEVEL_L0

    # --- similarity -------------------------------------------------------
    similar: list[Any] = []
    try:
        similar = await similarity_mod.find_similar_tenders(
            conn, tender=tender, as_of=cutoff
        )
    except Exception as exc:
        log.exception("similarity failed for tender %s", tender_id)
        # Caps at L0, the harshest cap in this module. Retrieval is upstream of
        # every number: the market sample is drawn from it, the explanation cites
        # it, and the market model would only be re-running the same broken query
        # itself. A price with no retrievable comparables behind it is exactly the
        # unsupported number this engine exists to refuse.
        degradations.append(
            _Degradation(
                STAGE_SIMILARITY, SuppressionReason.MODEL_UNAVAILABLE, str(exc), LEVEL_L0
            )
        )

    # --- market -----------------------------------------------------------
    market: PricePrediction
    try:
        market = await market_mod.market_quantiles(
            conn, tender=tender, as_of=cutoff, similar=similar or None
        )
    except Exception as exc:
        log.exception("market model failed for tender %s", tender_id)
        market = PricePrediction.suppressed(
            tender_id=int(tender["id"]),
            scope=PredictionScope.MARKET,
            reason=SuppressionReason.MODEL_UNAVAILABLE,
        )
        degradations.append(
            _Degradation(
                STAGE_MARKET, SuppressionReason.MODEL_UNAVAILABLE, str(exc), LEVEL_L0
            )
        )

    tier = market.evidence_tier
    if not market.is_suppressed:
        level = LEVEL_L1

    # --- competitors ------------------------------------------------------
    competitors: list[dict[str, Any]] = []
    if include_competitors and not market.is_suppressed:
        competitors, comp_level, comp_degradations = await _competitor_field(
            conn,
            tender=tender,
            cutoff=cutoff,
            market=market,
            competitor_mod=competitor_mod,
            participation_mod=participation_mod,
        )
        degradations.extend(comp_degradations)
        level = max(level, comp_level, key=LEVELS.index)

    for degradation in degradations:
        level = _cap(level, degradation.caps_level)

    # --- explanation ------------------------------------------------------
    explanation: dict[str, Any]
    try:
        explanation = await explain_mod.build_explanation(
            conn, prediction=market, similar=similar, competitors=competitors
        )
    except Exception as exc:
        log.exception("explanation failed for tender %s", tender_id)
        explanation = {
            "observed": [],
            "derived": [],
            "predicted": [],
            "drivers": [],
            "data_quality_warnings": ["تعذّر بناء تفسير هذا التقدير"],
            "suppression": {
                "reason": SuppressionReason.MODEL_UNAVAILABLE.value,
                "scope": PredictionScope.MARKET.value,
            },
        }
        degradations.append(
            _Degradation(
                STAGE_EXPLANATION, SuppressionReason.MODEL_UNAVAILABLE, str(exc), LEVEL_L4
            )
        )

    suppression: dict[str, Any] | None = None
    if market.is_suppressed:
        suppression = {
            "reason": market.suppression_reason.value,
            "scope": PredictionScope.MARKET.value,
            "stage": STAGE_MARKET,
            "evidence_tier": tier.value if tier else None,
            "evidence_count": market.evidence_count,
        }
    elif degradations:
        first = degradations[0]
        suppression = {
            "reason": first.reason.value,
            "scope": PredictionScope.MARKET.value,
            "stage": first.stage,
            "evidence_tier": tier.value if tier else None,
            "evidence_count": market.evidence_count,
        }

    return {
        "tender": _tender_summary(tender, cutoff),
        "tenant_id": int(tenant_id),
        "as_of": cutoff.isoformat(),
        "market": market.to_dict(),
        "evidence_tier": tier.value if tier else None,
        "allowed_level": level,
        # Serialised last: the sub-models above consume the PricePrediction
        # objects, but what leaves this function is JSON-safe end to end, so the
        # API never has to know which values are contract objects.
        "competitors": [
            {**entry, "prediction": entry["prediction"].to_dict()} for entry in competitors
        ],
        "similar_tenders": [
            item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in similar
        ],
        "explanation": explanation,
        "suppression": suppression,
        "degradations": [d.to_dict() for d in degradations],
        "generated_at": datetime.now(UTC).isoformat(),
        "model_version": MODEL_VERSION,
    }


async def _competitor_field(
    conn: Any,
    *,
    tender: Mapping[str, Any],
    cutoff: datetime,
    market: PricePrediction,
    competitor_mod: Any,
    participation_mod: Any,
    limit: int = DEFAULT_COMPETITOR_LIMIT,
) -> tuple[list[dict[str, Any]], str, list[_Degradation]]:
    """Candidate bidders with their participation odds and, where the evidence
    allows it, a price band each.

    Returns ``(competitors, level_reached, degradations)``. A vendor that cannot
    be priced is still returned with a **suppressed** prediction: "this vendor
    operates here and we cannot put a number on their bid" is information, and
    dropping them would show the user a smaller field than the real one.
    """
    degradations: list[_Degradation] = []
    try:
        candidates = await participation_mod.candidate_bidders(
            conn, tender=tender, as_of=cutoff, limit=limit
        )
    except Exception as exc:
        log.exception("participation failed for tender %s", tender.get("id"))
        return (
            [],
            LEVEL_L1,
            [
                _Degradation(
                    STAGE_PARTICIPATION,
                    SuppressionReason.MODEL_UNAVAILABLE,
                    str(exc),
                    LEVEL_L1,
                )
            ],
        )

    if not candidates:
        return [], LEVEL_L1, []

    names = await _vendor_names(conn, [c.vendor_id for c in candidates])
    reference_price = _as_float(market.p50)

    competitors: list[dict[str, Any]] = []
    level = LEVEL_L1
    for candidate in candidates:
        if not candidate.is_suppressed:
            level = max(level, LEVEL_L2, key=LEVELS.index)
        try:
            prediction = await competitor_mod.competitor_quantiles(
                conn,
                vendor_id=candidate.vendor_id,
                tender=dict(tender),
                as_of=cutoff,
                market=market,
            )
        except Exception as exc:
            log.exception(
                "competitor model failed for vendor %s on tender %s",
                candidate.vendor_id,
                tender.get("id"),
            )
            degradations.append(
                _Degradation(
                    STAGE_COMPETITOR,
                    SuppressionReason.MODEL_UNAVAILABLE,
                    f"vendor {candidate.vendor_id}: {exc}",
                    LEVEL_L2,
                )
            )
            prediction = PricePrediction.suppressed(
                tender_id=int(tender["id"]),
                scope=PredictionScope.COMPETITOR,
                reason=SuppressionReason.MODEL_UNAVAILABLE,
                subject_id=candidate.vendor_id,
            )

        if not prediction.is_suppressed:
            level = max(level, LEVEL_L3, key=LEVELS.index)
            if prediction.evidence_tier is EvidenceTier.A:
                level = max(level, LEVEL_L4, key=LEVELS.index)

        risk = None
        if reference_price is not None and not prediction.is_suppressed:
            # Undercut risk *at the market median*, which is the only reference
            # price that exists before the user has typed a bid. It is a property
            # of the modelled distribution, never a claim about the vendor.
            risk = competitor_mod.undercut_risk(reference_price, prediction)

        competitors.append(
            {
                "vendor_id": candidate.vendor_id,
                "name": names.get(candidate.vendor_id),
                "participation": candidate.to_dict(),
                "prediction": prediction,
                "undercut_risk": risk,
                "undercut_risk_reference_price": reference_price if risk is not None else None,
            }
        )
    return competitors, level, degradations


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


async def persist_prediction(
    conn: Any,
    prediction: PricePrediction,
    *,
    origin: str = "interactive",
    snapshot_date: Any = None,
) -> int | None:
    """Insert one ``PricePrediction`` and return its row id (None on failure).

    Never raises: a scenario the user can see but that we failed to record is a
    bookkeeping problem, not a reason to lose their answer. The failure is logged
    and reported to the caller as a degradation.
    """
    try:
        return await conn.fetchval(
            _INSERT_PREDICTION_SQL,
            int(prediction.tender_id),
            prediction.prediction_scope.value,
            _as_int(prediction.subject_id),
            _numeric(prediction.p10),
            _numeric(prediction.p50),
            _numeric(prediction.p90),
            _numeric(prediction.expected_value),
            _numeric(prediction.win_probability),
            prediction.confidence_score,
            prediction.similarity_confidence,
            prediction.data_freshness_score,
            int(prediction.evidence_count),
            prediction.evidence_tier.value if prediction.evidence_tier else None,
            prediction.model_version,
            prediction.feature_snapshot_id,
            prediction.seed,
            prediction.suppression_reason.value if prediction.suppression_reason else None,
            json.dumps([f.to_dict() for f in prediction.explanation_factors], ensure_ascii=False),
            prediction.generated_at,
            origin,
            snapshot_date,
        )
    except Exception:
        log.exception("failed to persist prediction")
        return None


async def persist_scenario(
    conn: Any,
    *,
    tenant_id: int,
    tender_id: int,
    estimated_cost: float,
    min_margin_pct: float,
    target_win_pct: float | None,
    proposed_bid: float | None,
    risk_reserve: float,
    seed: int,
    name: str = "",
    created_by: str | None = None,
) -> dict[str, Any]:
    """Insert a **new version** of the tenant's scenario for this tender.

    Scenarios are append-only (US-03): the previous version keeps its inputs and
    its numbers, and only gains a ``superseded_by`` pointer to the new row. A user
    who reruns with a different cost must be able to see what the old answer was
    and what changed — an UPDATE would destroy exactly that.

    Returns ``{'id', 'version', 'superseded_id', 'created_at'}``; ``id`` is None
    when the insert failed, which the caller reports as a degradation rather than
    an exception.
    """
    previous_id: int | None = None
    version = 1
    try:
        row = await conn.fetchrow(_LATEST_SCENARIO_SQL, int(tenant_id), int(tender_id))
        if row is not None:
            previous = dict(row)
            previous_id = _as_int(previous.get("id"))
            version = (_as_int(previous.get("version")) or 0) + 1
    except Exception as exc:  # noqa: BLE001 - fall back to version 1
        log.warning("scenario version lookup failed: %s", exc)

    try:
        inserted = await conn.fetchrow(
            _INSERT_SCENARIO_SQL,
            int(tenant_id),
            int(tender_id),
            name,
            _numeric(estimated_cost),
            _numeric(min_margin_pct),
            _numeric(target_win_pct),
            _numeric(proposed_bid),
            _numeric(risk_reserve),
            version,
            int(seed),
            created_by,
        )
    except Exception:
        log.exception("failed to persist scenario")
        return {"id": None, "version": version, "superseded_id": None, "created_at": None}

    created = dict(inserted)
    new_id = _as_int(created.get("id"))
    if previous_id is not None and new_id is not None:
        try:
            await conn.execute(_SUPERSEDE_SCENARIO_SQL, previous_id, new_id)
        except Exception as exc:  # noqa: BLE001 - the new version is already safe
            log.warning("failed to mark scenario %s superseded: %s", previous_id, exc)
    return {
        "id": new_id,
        "version": _as_int(created.get("version")) or version,
        "superseded_id": previous_id,
        "created_at": _iso(created.get("created_at")),
    }


# --------------------------------------------------------------------------
# scenario simulation
# --------------------------------------------------------------------------


def build_price_grid(
    market: PricePrediction,
    *,
    low_factor: float = GRID_LOW_FACTOR,
    high_factor: float = GRID_HIGH_FACTOR,
    points: int = GRID_POINTS,
) -> list[float]:
    """Evenly spaced candidate prices spanning the market range.

    ``low_factor * p10`` to ``high_factor * p90``: wide enough that the optimum
    is interior rather than pinned to an endpoint, and anchored to the observed
    market rather than to the user's cost, so the curve means the same thing for
    every user looking at the same tender. Returns ``[]`` for a suppressed market
    — there is no honest range to span.
    """
    if market.is_suppressed:
        return []
    p10 = _as_float(market.p10)
    p90 = _as_float(market.p90)
    if p10 is None or p90 is None or points < 2:
        return []
    low = max(1.0, low_factor * p10)
    high = max(low * (1.0 + 1e-9), high_factor * p90)
    step = (high - low) / (points - 1)
    return [low + step * i for i in range(points)]


def _coerce_prediction(value: Any) -> PricePrediction | None:
    """A ``PricePrediction`` from either the object or its ``to_dict`` form."""
    if isinstance(value, PricePrediction):
        return value
    if isinstance(value, Mapping):
        try:
            return _prediction_from_dict(value)
        except (KeyError, ValueError) as exc:
            log.warning("could not read a prediction back from its dict form: %s", exc)
    return None


def build_competitor_draws(
    competitors: Sequence[Mapping[str, Any]],
    *,
    technical_pass_p: float = DEFAULT_TECHNICAL_PASS_P,
) -> list[Any]:
    """Turn priced competitors into Monte Carlo draws.

    Only vendors with **both** a supportable participation probability and a
    supportable price band become draws. A vendor missing either is left out
    entirely rather than given a default: a made-up participant would change the
    win probability of every price on the curve, which is precisely the number
    the user is about to act on.

    Accepts either the contract objects or the JSON form ``tender_intelligence``
    returns, so a caller can hand back the payload it was given.
    """
    from . import competitor as competitor_mod
    from .montecarlo import CompetitorDraw

    draws: list[Any] = []
    for entry in competitors or []:
        prediction = _coerce_prediction(entry.get("prediction"))
        if prediction is None or prediction.is_suppressed:
            continue
        participation = entry.get("participation") or {}
        probability = _as_float(
            participation.get("probability") if isinstance(participation, Mapping) else None
        )
        if probability is None:
            continue
        p50 = _as_float(prediction.p50)
        if p50 is None or p50 <= 0:
            continue
        sigma = competitor_mod.implied_sigma(prediction) or 0.0
        draws.append(
            CompetitorDraw(
                vendor_id=int(prediction.subject_id or entry.get("vendor_id") or 0),
                participation_p=probability,
                log_mu=math.log(p50),
                log_sigma=max(0.0, sigma),
                technical_pass_p=technical_pass_p,
            )
        )
    draws.sort(key=lambda d: d.vendor_id)
    return draws


async def scenario_simulation(
    conn: Any,
    *,
    tender_id: int,
    tenant_id: int,
    estimated_cost: float,
    min_margin_pct: float,
    target_win_pct: float | None = None,
    proposed_bid: float | None = None,
    seed: int,
    risk_reserve: float = 0.0,
    iterations: int = 4000,
) -> dict[str, Any]:
    """Price the operator's own bid: win-probability curve, optimum, and a record.

    Runs the Monte Carlo curve over a market-anchored price grid, optimises
    against the tenant's cost and margin constraints, persists the resulting
    ``USER_OPTIMIZER`` prediction and a **new version** of the scenario row, and
    returns everything needed to reproduce the run exactly: the seed, the
    iteration count, the model version and the market's ``feature_snapshot_id``.

    Suppresses rather than guesses in two cases, both common with this data:
    a suppressed market (no grid to span) and an empty competitor field (a win
    probability computed against nobody is a number about nothing). In both the
    scenario row is still written, because the user's inputs are worth keeping
    even when the engine cannot answer them.
    """
    from . import montecarlo
    from . import optimizer as optimizer_mod
    from .optimizer import OptimizerConstraints

    generated_at = datetime.now(UTC)
    intelligence = await tender_intelligence(
        conn, tender_id=tender_id, tenant_id=tenant_id, include_competitors=True
    )
    market_dict = intelligence["market"]
    market = _prediction_from_dict(market_dict)
    degradations: list[_Degradation] = [
        _Degradation(
            stage=d["stage"],
            reason=SuppressionReason(d["reason"]),
            detail=d["detail"],
            caps_level=d["caps_level"],
        )
        for d in intelligence["degradations"]
    ]

    scenario_row = await persist_scenario(
        conn,
        tenant_id=tenant_id,
        tender_id=tender_id,
        estimated_cost=estimated_cost,
        min_margin_pct=min_margin_pct,
        target_win_pct=target_win_pct,
        proposed_bid=proposed_bid,
        risk_reserve=risk_reserve,
        seed=seed,
    )
    if scenario_row["id"] is None:
        degradations.append(
            _Degradation(
                STAGE_PERSISTENCE,
                SuppressionReason.MODEL_UNAVAILABLE,
                "scenario row could not be written",
                LEVEL_L4,
            )
        )

    grid = build_price_grid(market)
    draws = build_competitor_draws(intelligence["competitors"])

    assumptions = {
        "technical_pass_probability": {
            "value": DEFAULT_TECHNICAL_PASS_P,
            "kind": "assumption",
            "detail": (
                "no technical evaluation outcomes are recorded in this dataset; "
                "this is a stated assumption, not a measurement"
            ),
        },
        "evaluation_rule": montecarlo.EVALUATION_RULE_LOWEST_QUALIFIED,
        "grid_low_factor": GRID_LOW_FACTOR,
        "grid_high_factor": GRID_HIGH_FACTOR,
        "competitor_draws": [d.to_dict() for d in draws],
    }

    curve: list[Any] = []
    optimizer_result: Any = None
    prediction: PricePrediction
    suppression: dict[str, Any] | None = None

    if not grid:
        suppression = {
            "reason": (
                market.suppression_reason.value
                if market.suppression_reason
                else SuppressionReason.NO_COMPARABLE_TENDERS.value
            ),
            "scope": PredictionScope.USER_OPTIMIZER.value,
            "detail": "no supportable market range to build a price grid from",
        }
        prediction = PricePrediction.suppressed(
            tender_id=int(tender_id),
            scope=PredictionScope.USER_OPTIMIZER,
            reason=SuppressionReason(suppression["reason"]),
            evidence_count=market.evidence_count,
            evidence_tier=market.evidence_tier,
            seed=seed,
            feature_snapshot_id=market.feature_snapshot_id,
            generated_at=generated_at,
        )
    elif not draws:
        suppression = {
            "reason": SuppressionReason.INSUFFICIENT_EVIDENCE.value,
            "scope": PredictionScope.USER_OPTIMIZER.value,
            "detail": (
                "no candidate vendor has both a supportable participation probability "
                "and a supportable price band; a win-probability curve against an "
                "empty field would be a number about nobody"
            ),
        }
        prediction = PricePrediction.suppressed(
            tender_id=int(tender_id),
            scope=PredictionScope.USER_OPTIMIZER,
            reason=SuppressionReason.INSUFFICIENT_EVIDENCE,
            evidence_count=market.evidence_count,
            evidence_tier=market.evidence_tier,
            seed=seed,
            feature_snapshot_id=market.feature_snapshot_id,
            generated_at=generated_at,
        )
    else:
        try:
            curve = montecarlo.win_probability_curve(
                price_grid=grid,
                competitors=draws,
                seed=seed,
                iterations=iterations,
                user_technical_pass_p=DEFAULT_TECHNICAL_PASS_P,
            )
        except Exception as exc:
            log.exception("simulation failed for tender %s", tender_id)
            curve = []
            degradations.append(
                _Degradation(
                    STAGE_SIMULATION, SuppressionReason.MODEL_UNAVAILABLE, str(exc), LEVEL_L1
                )
            )

        constraints = OptimizerConstraints(
            estimated_cost=float(estimated_cost),
            min_margin_pct=float(min_margin_pct),
            target_win_probability=(
                None if target_win_pct is None else float(target_win_pct) / 100.0
            ),
            risk_reserve=float(risk_reserve),
        )
        if curve:
            try:
                optimizer_result = optimizer_mod.optimize(curve=curve, constraints=constraints)
            except Exception as exc:
                log.exception("optimizer failed for tender %s", tender_id)
                optimizer_result = None
                degradations.append(
                    _Degradation(
                        STAGE_OPTIMIZER,
                        SuppressionReason.MODEL_UNAVAILABLE,
                        str(exc),
                        LEVEL_L1,
                    )
                )

        if optimizer_result is None:
            suppression = {
                "reason": SuppressionReason.MODEL_UNAVAILABLE.value,
                "scope": PredictionScope.USER_OPTIMIZER.value,
                "detail": "the optimizer produced no result for this scenario",
            }
            prediction = PricePrediction.suppressed(
                tender_id=int(tender_id),
                scope=PredictionScope.USER_OPTIMIZER,
                reason=SuppressionReason.MODEL_UNAVAILABLE,
                evidence_count=market.evidence_count,
                evidence_tier=market.evidence_tier,
                seed=seed,
                feature_snapshot_id=market.feature_snapshot_id,
                generated_at=generated_at,
            )
        else:
            prediction = optimizer_mod.to_price_prediction(
                optimizer_result,
                tender_id=int(tender_id),
                constraints=constraints,
                evidence_count=market.evidence_count,
                evidence_tier=market.evidence_tier,
                confidence_score=market.confidence_score,
                similarity_confidence=market.similarity_confidence,
                seed=seed,
                feature_snapshot_id=market.feature_snapshot_id,
                generated_at=generated_at,
            )
            if prediction.is_suppressed:
                suppression = {
                    "reason": prediction.suppression_reason.value,
                    "scope": PredictionScope.USER_OPTIMIZER.value,
                    "detail": optimizer_result.infeasible_reason or "no feasible bid",
                }

    prediction_id = await persist_prediction(conn, prediction)
    if prediction_id is None:
        degradations.append(
            _Degradation(
                STAGE_PERSISTENCE,
                SuppressionReason.MODEL_UNAVAILABLE,
                "USER_OPTIMIZER prediction row could not be written",
                LEVEL_L4,
            )
        )

    proposed: dict[str, Any] | None = None
    if proposed_bid is not None and draws:
        try:
            outcome = montecarlo.simulate(
                user_bid=float(proposed_bid),
                competitors=draws,
                seed=seed,
                iterations=iterations,
                user_technical_pass_p=DEFAULT_TECHNICAL_PASS_P,
            )
            proposed = {"bid": float(proposed_bid), **outcome.to_dict()}
        except Exception as exc:
            log.exception("proposed-bid simulation failed for tender %s", tender_id)
            degradations.append(
                _Degradation(
                    STAGE_SIMULATION, SuppressionReason.MODEL_UNAVAILABLE, str(exc), LEVEL_L4
                )
            )
    elif proposed_bid is not None:
        proposed = {
            "bid": float(proposed_bid),
            "win_probability": None,
            "suppression_reason": SuppressionReason.INSUFFICIENT_EVIDENCE.value,
        }

    level = intelligence["allowed_level"]
    for degradation in degradations:
        level = _cap(level, degradation.caps_level)

    return {
        "tender_id": int(tender_id),
        "tenant_id": int(tenant_id),
        "tender": intelligence["tender"],
        "market": market_dict,
        "evidence_tier": intelligence["evidence_tier"],
        "allowed_level": level,
        "inputs": {
            "estimated_cost": float(estimated_cost),
            "min_margin_pct": float(min_margin_pct),
            "target_win_pct": None if target_win_pct is None else float(target_win_pct),
            "proposed_bid": None if proposed_bid is None else float(proposed_bid),
            "risk_reserve": float(risk_reserve),
            "kind": "user_input",
        },
        "price_grid": grid,
        "curve": [point.to_dict() for point in curve],
        "optimizer": optimizer_result.to_dict() if optimizer_result is not None else None,
        "prediction": prediction.to_dict(),
        "prediction_id": prediction_id,
        "proposed_bid_simulation": proposed,
        "scenario": scenario_row,
        "assumptions": assumptions,
        "suppression": suppression,
        "degradations": [d.to_dict() for d in degradations],
        "seed": int(seed),
        "iterations": int(iterations),
        "feature_snapshot_id": market.feature_snapshot_id,
        "generated_at": generated_at.isoformat(),
        "model_version": MODEL_VERSION,
    }


def _prediction_from_dict(payload: Mapping[str, Any]) -> PricePrediction:
    """Rebuild a ``PricePrediction`` from its ``to_dict`` form.

    ``tender_intelligence`` returns JSON-safe dicts because that is what the API
    serves; the scenario path needs the object back to read its tier and snapshot
    id. Round-tripping through the contract keeps its invariants applied to the
    value the API actually returned, rather than to a parallel copy.
    """
    generated = payload.get("generated_at")
    if isinstance(generated, str):
        generated = datetime.fromisoformat(generated)
    return PricePrediction(
        tender_id=int(payload["tender_id"]),
        prediction_scope=payload["prediction_scope"],
        subject_id=payload.get("subject_id"),
        p10=payload.get("p10"),
        p50=payload.get("p50"),
        p90=payload.get("p90"),
        expected_value=payload.get("expected_value"),
        win_probability=payload.get("win_probability"),
        confidence_score=payload.get("confidence_score"),
        similarity_confidence=payload.get("similarity_confidence"),
        data_freshness_score=payload.get("data_freshness_score"),
        evidence_count=int(payload.get("evidence_count") or 0),
        evidence_tier=payload.get("evidence_tier"),
        model_version=payload.get("model_version") or MODEL_VERSION,
        feature_snapshot_id=payload.get("feature_snapshot_id"),
        seed=payload.get("seed"),
        generated_at=generated or datetime.now(UTC),
        explanation_factors=[
            ExplanationFactor(
                name=factor["name"],
                direction=factor["direction"],
                weight=factor["weight"],
                kind=factor["kind"],
                detail=factor.get("detail", ""),
                evidence_ref=factor.get("evidence_ref"),
            )
            for factor in payload.get("explanation_factors") or []
        ],
        suppression_reason=payload.get("suppression_reason"),
    )


__all__ = [
    "DEFAULT_COMPETITOR_LIMIT",
    "DEFAULT_TECHNICAL_PASS_P",
    "GRID_HIGH_FACTOR",
    "GRID_LOW_FACTOR",
    "GRID_POINTS",
    "LEVELS",
    "LEVEL_L0",
    "LEVEL_L1",
    "LEVEL_L2",
    "LEVEL_L3",
    "LEVEL_L4",
    "TenderNotFound",
    "build_competitor_draws",
    "build_price_grid",
    "load_tender",
    "persist_prediction",
    "persist_scenario",
    "resolve_as_of",
    "scenario_simulation",
    "tender_intelligence",
]
