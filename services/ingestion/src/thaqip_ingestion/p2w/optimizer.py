"""Bid optimizer: maximise expected contribution subject to HARD constraints.

PRD FR-010 / FR-011, architecture section 12.

The model
--------
The optimizer takes a *win-probability curve* — the output of
``thaqip_ingestion.p2w.montecarlo.win_probability_curve``, i.e. a set of
(price, P(win|price)) points produced by one common-random-numbers simulation —
and the operator's own cost inputs, and returns the single bid that maximises

    objective(b) = P(win | b) * (b - estimated_cost - risk_reserve)

over the **feasible set** only. Between two simulated grid prices the curve is
read by linear interpolation, so the objective is a piecewise quadratic and its
maximum on every piece is available in closed form (the parabola's vertex,
clamped to the piece). That is why this module does not do a grid search: the
returned optimum is exact for the interpolated curve, not "the best of the
points we happened to try".

The feasible set is

    b >= hard_cost_floor                     (if given)
    b >= the price at which margin_pct(b) >= min_margin_pct
    b <= max_bid                             (if given)
    P(win | b) >= target_win_probability     (if given)
    grid_min <= b <= grid_max                (never extrapolate off the curve)

If that set is empty the optimizer returns ``recommended_bid=None`` and an
``infeasible_reason`` naming the constraints that conflict. **It never returns
the closest violating price.** "Recommends below a hard cost floor or below the
minimum margin" is an automatic NO-GO red gate for this product, so feasibility
is enforced three times over: candidates are only ever generated inside the
feasible segments, the margin floor price is nudged upward until it *provably*
satisfies ``margin_pct`` in floating point, and a final independent check
re-verifies the returned bid against every constraint and downgrades the result
to an infeasible one if that check ever fails.

Honest limitations
------------------
* Everything here is conditional on the curve it is handed. If the simulation
  behind the curve is built on thin competitor evidence, the "optimal" bid is
  precise about an uncertain input. Callers must carry the curve's evidence tier
  through to the UI; this module deliberately has no way to invent one.
* The optimum is never extrapolated beyond the simulated price grid. When the
  best feasible price sits at ``grid_min``/``grid_max`` that is reported in
  ``binding_constraints`` as grid censoring, not silently returned as an
  interior optimum.
* ``risk_reserve`` reduces the contribution but does **not** relax or tighten the
  margin constraint: ``min_margin_pct`` is measured against ``estimated_cost``
  alone, exactly as the PRD defines it.
* Expected contribution can legitimately be negative (e.g. a large risk reserve
  with a low minimum margin). That is reported, not suppressed — the constraints
  are the user's, and hiding the sign would be worse than showing it.
* The optimizer is pure: no database, no clock, no randomness. Determinism comes
  for free, and the seed that matters is the simulation's, not this module's.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .contracts import (
    MODEL_VERSION,
    EvidenceTier,
    ExplanationFactor,
    PredictionScope,
    PricePrediction,
    Quantiles,
    SuppressionReason,
    freshness_score,
)

# --- Named constants ---------------------------------------------------------

#: A bid is "safe" when the simulation gives it at least this win probability.
#: 0.60 is a deliberate product choice, not an estimate: below a coin flip a
#: range cannot honestly be called safe, and 0.60 keeps the safe band from
#: collapsing to a single point on the flat top of a typical curve.
SAFE_WIN_FLOOR = 0.60

#: "Top win-probability decile" for the aggressive range: prices whose win
#: probability lies within the top 10% of the win-probability SPAN attainable
#: inside the feasible set. Defined on the attainable span rather than on the
#: absolute [0,1] scale so the band stays meaningful for curves that never get
#: near 1.0 — which, with this corpus, is most of them.
AGGRESSIVE_TOP_DECILE = 0.10

#: Margin is measured on revenue: (bid - cost) / bid. A required margin of 100%
#: or more is unreachable at any finite price with a positive cost.
MAX_MARGIN_PCT = 100.0

#: Relative tolerance used only to decide whether the optimum *sits on* a
#: constraint (for reporting in ``binding_constraints``). It never widens the
#: feasible set: feasibility itself is tested with exact comparisons.
BOUNDARY_REL_TOL = 1e-9

#: Two candidate objectives closer than this (relative to the better one) count
#: as a tie; ties are resolved toward the LOWER price, i.e. toward the bid that
#: wins more often for the same expected contribution.
TIE_REL_TOL = 1e-12

#: Upper bound on the number of upward nudges used to force the computed margin
#: floor price to satisfy ``margin_pct`` in floating point. The nudge starts at
#: one ULP and DOUBLES each step, so 64 steps span every increment that can
#: matter (a single-ULP walk cannot close the gap when ``min_margin_pct`` is
#: near 100%, where the residual is computed with ~1e-16 absolute error). The
#: loop is bounded so a pathological input cannot hang the optimizer.
MAX_FLOOR_NUDGE_STEPS = 64

CONSTRAINT_HARD_COST_FLOOR = "hard_cost_floor"
CONSTRAINT_MIN_MARGIN = "min_margin_pct"
CONSTRAINT_MAX_BID = "max_bid"
CONSTRAINT_TARGET_WIN_PROBABILITY = "target_win_probability"
CONSTRAINT_GRID_LOWER = "price_grid_lower_bound"
CONSTRAINT_GRID_UPPER = "price_grid_upper_bound"


class OptimizerError(ValueError):
    """Invalid optimizer input. A subclass of ValueError, matching `contracts`."""


# --- Validation helpers ------------------------------------------------------


def _require_finite(name: str, value: Any) -> float:
    """Coerce to a finite float or raise OptimizerError naming the field.

    ``None``, ``NaN``, ``inf`` and non-numeric types all raise: the optimizer is
    a red-gate module and must refuse to run on an input it cannot reason about
    rather than propagate a NaN into a recommended price.
    """
    if value is None:
        raise OptimizerError(f"{name} must not be None")
    if isinstance(value, bool):
        raise OptimizerError(f"{name} must be a number, got bool")
    if isinstance(value, (str, bytes, bytearray)):
        # A numeric string means a type error upstream. Money must not be parsed
        # by accident here; Decimal (what asyncpg returns for numeric) is fine.
        raise OptimizerError(f"{name} must be a number, got {type(value).__name__}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OptimizerError(f"{name} must be a number, got {type(value).__name__}") from exc
    if not math.isfinite(number):
        raise OptimizerError(f"{name} must be finite, got {value!r}")
    return number


def _require_probability(name: str, value: Any) -> float:
    number = _require_finite(name, value)
    if not 0.0 <= number <= 1.0:
        raise OptimizerError(f"{name} must be within [0, 1], got {number}")
    return number


def _require_price(name: str, value: Any) -> float:
    number = _require_finite(name, value)
    if number <= 0.0:
        raise OptimizerError(f"{name} must be positive, got {number}")
    return number


# --- Public API --------------------------------------------------------------


def margin_pct(bid: float, cost: float) -> float:
    """Gross margin as a percentage of the bid (revenue margin).

    ``(bid - cost) / bid * 100``. Revenue margin rather than markup because that
    is how the tender teams quote it and how ``min_margin_pct`` is written in the
    PRD. Raises OptimizerError for a non-positive bid, a negative cost, or any
    non-finite input — a margin computed from NaN is worse than no margin.
    """
    bid_value = _require_price("bid", bid)
    cost_value = _require_finite("cost", cost)
    if cost_value < 0.0:
        raise OptimizerError(f"cost must not be negative, got {cost_value}")
    return (bid_value - cost_value) / bid_value * 100.0


@dataclass
class OptimizerConstraints:
    """The operator's hard constraints. Every one of these is a hard gate.

    ``estimated_cost``  delivery cost the bid must cover (tenant-private input).
    ``min_margin_pct``  minimum revenue margin, in percent, measured on
                        ``estimated_cost`` alone.
    ``hard_cost_floor`` an absolute price the bid may never go below, e.g. a
                        board-approved floor. ``None`` means no separate floor.
    ``target_win_probability`` optional floor on P(win|b) from the simulation.
    ``risk_reserve``    money set aside for risk; subtracted from contribution
                        but NOT from the margin test (see the module docstring).
    ``max_bid``         optional ceiling, e.g. an owner's published budget.

    Validation runs in ``__post_init__`` *and* again inside ``optimize`` because
    the dataclass is mutable and a red-gate module cannot trust that it was not
    edited after construction.
    """

    estimated_cost: float
    min_margin_pct: float
    hard_cost_floor: float | None = None
    target_win_probability: float | None = None
    risk_reserve: float = 0.0
    max_bid: float | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> OptimizerConstraints:
        """Raise OptimizerError unless every field is usable. Returns self."""
        cost = _require_finite("estimated_cost", self.estimated_cost)
        if cost < 0.0:
            raise OptimizerError(f"estimated_cost must not be negative, got {cost}")
        _require_finite("min_margin_pct", self.min_margin_pct)
        reserve = _require_finite("risk_reserve", self.risk_reserve)
        if reserve < 0.0:
            raise OptimizerError(f"risk_reserve must not be negative, got {reserve}")
        if self.hard_cost_floor is not None:
            floor = _require_finite("hard_cost_floor", self.hard_cost_floor)
            if floor < 0.0:
                raise OptimizerError(f"hard_cost_floor must not be negative, got {floor}")
        if self.target_win_probability is not None:
            _require_probability("target_win_probability", self.target_win_probability)
        if self.max_bid is not None:
            _require_price("max_bid", self.max_bid)
        return self

    @property
    def contribution_base(self) -> float:
        """Cost + reserve: the point at which contribution turns positive."""
        return float(self.estimated_cost) + float(self.risk_reserve)

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimated_cost": float(self.estimated_cost),
            "min_margin_pct": float(self.min_margin_pct),
            "hard_cost_floor": None if self.hard_cost_floor is None else float(self.hard_cost_floor),
            "target_win_probability": (
                None
                if self.target_win_probability is None
                else float(self.target_win_probability)
            ),
            "risk_reserve": float(self.risk_reserve),
            "max_bid": None if self.max_bid is None else float(self.max_bid),
        }


@dataclass
class OptimizerResult:
    """The recommendation, or an explicit refusal to make one.

    Exactly one of two states, mirroring the suppression invariant in
    `contracts.PricePrediction`: either ``recommended_bid`` is a feasible price
    and ``infeasible_reason`` is None, or ``recommended_bid`` is None and
    ``infeasible_reason`` explains which constraints conflict.
    """

    recommended_bid: float | None = None
    expected_contribution: float | None = None
    win_probability: float | None = None
    margin_pct: float | None = None
    safe_range: tuple[float, float] | None = None
    aggressive_range: tuple[float, float] | None = None
    infeasible_reason: str | None = None
    evaluated_points: int = 0
    binding_constraints: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.recommended_bid is None and self.infeasible_reason is None:
            raise OptimizerError("a result without a bid must carry an infeasible_reason")
        if self.recommended_bid is not None and self.infeasible_reason is not None:
            raise OptimizerError("a result with a bid must not carry an infeasible_reason")

    @property
    def is_feasible(self) -> bool:
        return self.recommended_bid is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommended_bid": self.recommended_bid,
            "expected_contribution": self.expected_contribution,
            "win_probability": self.win_probability,
            "margin_pct": self.margin_pct,
            "safe_range": list(self.safe_range) if self.safe_range else None,
            "aggressive_range": list(self.aggressive_range) if self.aggressive_range else None,
            "infeasible_reason": self.infeasible_reason,
            "evaluated_points": self.evaluated_points,
            "binding_constraints": list(self.binding_constraints),
            "is_feasible": self.is_feasible,
        }


# --- Curve handling ----------------------------------------------------------


@dataclass(frozen=True)
class _Segment:
    """A price interval over which the interpolated win probability is linear.

    ``lo``/``hi`` are the (possibly clipped) endpoints actually in play; ``x0``,
    ``p0`` and ``slope`` describe the *whole* grid interval the segment came
    from, so clipping never changes the interpolation.
    """

    lo: float
    hi: float
    x0: float
    p0: float
    slope: float

    def probability_at(self, price: float) -> float:
        value = self.p0 + self.slope * (price - self.x0)
        # Interpolating between two values in [0,1] cannot leave [0,1] except by
        # rounding; clamp so downstream probability contracts always hold.
        return min(1.0, max(0.0, value))

    def clip(self, lo: float, hi: float) -> _Segment | None:
        new_lo = max(self.lo, lo)
        new_hi = min(self.hi, hi)
        if new_lo > new_hi:
            return None
        return _Segment(new_lo, new_hi, self.x0, self.p0, self.slope)


def _normalise_curve(curve: Iterable[Any]) -> list[tuple[float, float]]:
    """Sorted, de-duplicated (price, win_probability) pairs.

    Accepts montecarlo ``CurvePoint`` objects (duck-typed on the two attributes,
    so this module does not import montecarlo), mappings with ``price`` and
    ``win_probability`` keys, or 2-sequences — a curve that has been through
    JSON or the database is still usable. Duplicate prices are allowed only when
    they agree; a contradictory duplicate is an error, never a silent pick.
    """
    if isinstance(curve, (str, bytes)) or curve is None:
        raise OptimizerError("curve must be a sequence of curve points")
    seen: dict[float, float] = {}
    for item in curve:
        if isinstance(item, Mapping):
            if "price" not in item or "win_probability" not in item:
                raise OptimizerError(
                    "curve mapping entries need 'price' and 'win_probability' keys"
                )
            raw_price, raw_probability = item["price"], item["win_probability"]
        elif hasattr(item, "price") and hasattr(item, "win_probability"):
            raw_price, raw_probability = item.price, item.win_probability
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
            raw_price, raw_probability = item[0], item[1]
        else:
            raise OptimizerError(f"unsupported curve entry: {type(item).__name__}")
        price = _require_price("curve price", raw_price)
        probability = _require_probability("curve win_probability", raw_probability)
        previous = seen.get(price)
        if previous is not None and previous != probability:
            raise OptimizerError(
                f"curve has conflicting win probabilities at price {price}: "
                f"{previous} and {probability}"
            )
        seen[price] = probability
    if not seen:
        raise OptimizerError("curve must contain at least one point")
    return sorted(seen.items())


def _curve_segments(pairs: list[tuple[float, float]]) -> list[_Segment]:
    """One linear segment per adjacent pair; a single-point curve gives a point."""
    if len(pairs) == 1:
        price, probability = pairs[0]
        return [_Segment(price, price, price, probability, 0.0)]
    segments: list[_Segment] = []
    for index in range(len(pairs) - 1):
        x0, p0 = pairs[index]
        x1, p1 = pairs[index + 1]
        slope = (p1 - p0) / (x1 - x0)
        segments.append(_Segment(x0, x1, x0, p0, slope))
    return segments


def win_probability_at(curve: Iterable[Any], price: float) -> float:
    """Interpolated win probability at ``price``.

    Raises OptimizerError outside the simulated grid: off-grid the honest answer
    is "we did not simulate that", never an extrapolation.
    """
    pairs = _normalise_curve(curve)
    value = _require_price("price", price)
    if value < pairs[0][0] or value > pairs[-1][0]:
        raise OptimizerError(
            f"price {value} is outside the simulated grid "
            f"[{pairs[0][0]}, {pairs[-1][0]}]; the optimizer never extrapolates"
        )
    for segment in _curve_segments(pairs):
        if segment.lo <= value <= segment.hi:
            return segment.probability_at(value)
    raise OptimizerError(f"price {value} not covered by any curve segment")  # pragma: no cover


# --- Constraint geometry -----------------------------------------------------


def min_margin_price(cost: float, min_margin_pct_value: float) -> float | None:
    """Lowest price whose revenue margin reaches ``min_margin_pct_value``.

    ``b >= cost / (1 - m)``. Returns None when the requirement is unreachable at
    any finite price (m >= 100% with a positive cost). The closed-form value is
    then nudged upward by at most ``MAX_FLOOR_NUDGE_STEPS`` ULPs until
    ``margin_pct`` actually reports a compliant margin, so the floor is safe in
    floating point and not merely in algebra.
    """
    cost_value = _require_finite("cost", cost)
    if cost_value < 0.0:
        raise OptimizerError(f"cost must not be negative, got {cost_value}")
    margin = _require_finite("min_margin_pct", min_margin_pct_value)
    if margin >= MAX_MARGIN_PCT:
        # 100% margin means zero cost. With a positive cost it is unreachable;
        # with a zero cost every positive price already has 100% margin.
        return None if cost_value > 0.0 else 0.0
    if cost_value == 0.0:
        # Any positive price yields a 100% margin, which clears any m < 100.
        return 0.0
    price = cost_value / (1.0 - margin / 100.0)
    if price <= 0.0:
        # Only reachable for a very negative min_margin_pct; such a floor does
        # not bind at all, so report "no floor" rather than a negative price.
        return 0.0
    # Nudge upward until ``margin_pct`` actually reports a compliant margin.
    # The step GROWS geometrically from one ULP: for a margin close to 100% the
    # residual ``1 - cost/price`` is computed with an absolute error near
    # 1e-16, which is many orders of magnitude larger than the change a handful
    # of single-ULP steps can make, so a fixed ULP walk cannot converge there.
    # Doubling reaches any needed increment in at most 64 steps while
    # overshooting the true minimum floor by at most one step.
    step = math.ulp(price)
    for _ in range(MAX_FLOOR_NUDGE_STEPS):
        if margin_pct(price, cost_value) >= margin:
            return price
        nudged = price + step
        if not math.isfinite(nudged) or nudged <= price:
            break
        price = nudged
        step *= 2.0
    raise OptimizerError(  # pragma: no cover - defensive; never seen in practice
        f"could not compute a margin-safe floor for cost={cost_value} margin={margin}"
    )


def _feasible_segments(
    segments: list[_Segment],
    *,
    lo: float,
    hi: float,
    target: float | None,
) -> list[_Segment]:
    """Segments of ``[lo, hi]`` on which the win-probability target also holds.

    The target constraint is applied per segment where the probability is linear,
    so a non-monotone curve (which a finite simulation can produce) yields a
    correct union of intervals rather than a wrong single interval.
    """
    feasible: list[_Segment] = []
    for segment in segments:
        clipped = segment.clip(lo, hi)
        if clipped is None:
            continue
        if target is None:
            feasible.append(clipped)
            continue
        p_lo = clipped.probability_at(clipped.lo)
        p_hi = clipped.probability_at(clipped.hi)
        if p_lo >= target and p_hi >= target:
            feasible.append(clipped)
            continue
        if p_lo < target and p_hi < target:
            continue
        if clipped.slope == 0.0:  # pragma: no cover - equal endpoints handled above
            continue
        crossing = clipped.x0 + (target - clipped.p0) / clipped.slope
        crossing = min(clipped.hi, max(clipped.lo, crossing))
        piece = (
            _Segment(clipped.lo, crossing, clipped.x0, clipped.p0, clipped.slope)
            if p_lo >= target
            else _Segment(crossing, clipped.hi, clipped.x0, clipped.p0, clipped.slope)
        )
        # A crossing computed in floating point can land a hair on the wrong
        # side; trim the offending endpoint inward rather than trusting it.
        piece = _trim_to_target(piece, target)
        if piece is not None:
            feasible.append(piece)
    return feasible


def _trim_to_target(segment: _Segment, target: float) -> _Segment | None:
    """Pull an endpoint inward until both endpoints truly clear ``target``."""
    lo, hi = segment.lo, segment.hi
    for _ in range(MAX_FLOOR_NUDGE_STEPS):
        if segment.probability_at(lo) >= target:
            break
        if lo >= hi:
            return None
        lo = math.nextafter(lo, hi)
    else:  # pragma: no cover - defensive
        return None
    for _ in range(MAX_FLOOR_NUDGE_STEPS):
        if segment.probability_at(hi) >= target:
            break
        if hi <= lo:
            return None
        hi = math.nextafter(hi, lo)
    else:  # pragma: no cover - defensive
        return None
    if lo > hi:
        return None
    return _Segment(lo, hi, segment.x0, segment.p0, segment.slope)


def _merge(segments: list[_Segment]) -> list[tuple[float, float]]:
    """Merge touching/overlapping segments into plain (lo, hi) intervals."""
    if not segments:
        return []
    ordered = sorted((segment.lo, segment.hi) for segment in segments)
    merged: list[list[float]] = [list(ordered[0])]
    for lo, hi in ordered[1:]:
        if lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def _segment_optimum(segment: _Segment, base: float) -> list[float]:
    """Candidate prices that can maximise ``p(b) * (b - base)`` on a segment.

    ``p`` is linear here, so the objective is quadratic. With a negative slope
    it is a downward parabola whose vertex is the only interior candidate; with
    a non-negative slope the objective is non-decreasing where it matters and
    the maximum is at an endpoint. Endpoints are always candidates.
    """
    candidates = [segment.lo, segment.hi]
    if segment.slope < 0.0 and segment.hi > segment.lo:
        # p(b) = intercept + slope*b ; f(b) = (intercept + slope*b)(b - base)
        intercept = segment.p0 - segment.slope * segment.x0
        vertex = (segment.slope * base - intercept) / (2.0 * segment.slope)
        if segment.lo < vertex < segment.hi:
            candidates.append(vertex)
    return candidates


def _describe_bound(value: float, sources: list[tuple[str, float]]) -> str:
    """Name the constraint(s) whose bound equals ``value`` (several can tie)."""
    names = [name for name, bound in sources if bound == value]
    return " and ".join(names) if names else "the effective lower bound"


def _bound_label(name: str, constraints: OptimizerConstraints) -> str:
    """Constraint name with its configured value, for reason strings."""
    if name == CONSTRAINT_MIN_MARGIN:
        return f"{name} {_fmt(constraints.min_margin_pct)}"
    if name == CONSTRAINT_HARD_COST_FLOOR and constraints.hard_cost_floor is not None:
        return f"{name} {_fmt(constraints.hard_cost_floor)}"
    if name == CONSTRAINT_MAX_BID and constraints.max_bid is not None:
        return f"{name} {_fmt(constraints.max_bid)}"
    return name


def optimize(*, curve: Iterable[Any], constraints: OptimizerConstraints) -> OptimizerResult:
    """Best feasible bid for ``curve`` under ``constraints``.

    Returns an ``OptimizerResult``; when the feasible set is empty the result
    carries ``recommended_bid=None`` and a human-readable ``infeasible_reason``
    naming the conflicting constraints. A price outside the feasible set is never
    returned under any circumstances — see the module docstring.
    """
    if not isinstance(constraints, OptimizerConstraints):
        raise OptimizerError(
            f"constraints must be OptimizerConstraints, got {type(constraints).__name__}"
        )
    constraints.validate()

    pairs = _normalise_curve(curve)
    segments = _curve_segments(pairs)
    grid_lo, grid_hi = pairs[0][0], pairs[-1][0]

    cost = float(constraints.estimated_cost)
    base = constraints.contribution_base
    target = (
        None
        if constraints.target_win_probability is None
        else float(constraints.target_win_probability)
    )

    margin_floor = min_margin_price(cost, constraints.min_margin_pct)
    if margin_floor is None:
        return OptimizerResult(
            infeasible_reason=(
                f"min_margin_pct {_fmt(constraints.min_margin_pct)} is unreachable at any "
                f"price with estimated_cost {_fmt(cost)}: a margin of "
                f"{_fmt(MAX_MARGIN_PCT)}% or more requires a zero cost"
            ),
            evaluated_points=0,
        )

    hard_floor = 0.0 if constraints.hard_cost_floor is None else float(constraints.hard_cost_floor)
    lower_sources: list[tuple[str, float]] = [
        (CONSTRAINT_MIN_MARGIN, margin_floor),
        (CONSTRAINT_GRID_LOWER, grid_lo),
    ]
    if constraints.hard_cost_floor is not None:
        lower_sources.append((CONSTRAINT_HARD_COST_FLOOR, hard_floor))
    lower = max(margin_floor, hard_floor, grid_lo)

    upper_sources: list[tuple[str, float]] = [(CONSTRAINT_GRID_UPPER, grid_hi)]
    upper = grid_hi
    if constraints.max_bid is not None:
        upper = min(upper, float(constraints.max_bid))
        upper_sources.append((CONSTRAINT_MAX_BID, float(constraints.max_bid)))

    if lower > upper:
        return OptimizerResult(
            infeasible_reason=_price_conflict_reason(
                lower, upper, lower_sources, upper_sources, constraints, grid_lo, grid_hi
            ),
            evaluated_points=0,
        )

    feasible = _feasible_segments(segments, lo=lower, hi=upper, target=target)
    if not feasible:
        best_probability = _best_probability(
            _feasible_segments(segments, lo=lower, hi=upper, target=None)
        )
        binding_name = " and ".join(
            _bound_label(name, constraints)
            for name in _describe_bound(lower, lower_sources).split(" and ")
        )
        return OptimizerResult(
            infeasible_reason=(
                f"target_win_probability {_fmt(target)} unreachable at any price satisfying "
                f"{binding_name} (>= {_fmt(lower)}); the best attainable win probability in "
                f"[{_fmt(lower)}, {_fmt(upper)}] is {_fmt(best_probability)}"
            ),
            evaluated_points=0,
        )

    best_price: float | None = None
    best_objective = -math.inf
    evaluated = 0
    for segment in feasible:
        for candidate in _segment_optimum(segment, base):
            evaluated += 1
            objective = segment.probability_at(candidate) * (candidate - base)
            if best_price is None or _beats(objective, candidate, best_objective, best_price):
                best_objective, best_price = objective, candidate

    if best_price is None:  # pragma: no cover - feasible is non-empty here
        return OptimizerResult(
            infeasible_reason="no candidate price could be evaluated", evaluated_points=evaluated
        )

    # Final, independent gate. Everything above should already guarantee this;
    # if it ever does not, refuse rather than emit a non-compliant bid.
    violation = _verify(
        best_price,
        constraints=constraints,
        margin_floor=margin_floor,
        hard_floor=hard_floor if constraints.hard_cost_floor is not None else None,
        upper=upper,
        lower=lower,
        target=target,
        probability=win_probability_at(pairs, best_price),
    )
    if violation is not None:
        return OptimizerResult(
            infeasible_reason=(
                f"internal feasibility check rejected the computed optimum: {violation}"
            ),
            evaluated_points=evaluated,
        )

    probability = win_probability_at(pairs, best_price)
    safe = _range_for_threshold(feasible, SAFE_WIN_FLOOR, anchor=best_price)
    aggressive = _aggressive_range(feasible)

    return OptimizerResult(
        recommended_bid=best_price,
        expected_contribution=probability * (best_price - base),
        win_probability=probability,
        margin_pct=margin_pct(best_price, cost),
        safe_range=safe,
        aggressive_range=aggressive,
        infeasible_reason=None,
        evaluated_points=evaluated,
        binding_constraints=_binding_constraints(
            best_price,
            probability=probability,
            constraints=constraints,
            margin_floor=margin_floor,
            hard_floor=hard_floor if constraints.hard_cost_floor is not None else None,
            grid_lo=grid_lo,
            grid_hi=grid_hi,
            target=target,
        ),
    )


# --- Reporting helpers -------------------------------------------------------


def _fmt(value: float | None) -> str:
    """Compact, locale-free number formatting for reason strings."""
    if value is None:
        return "none"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6g}"


def _beats(objective: float, price: float, best_objective: float, best_price: float) -> bool:
    """Strictly better, or an exact-enough tie broken toward the lower price."""
    scale = max(abs(objective), abs(best_objective), 1.0)
    if objective > best_objective + TIE_REL_TOL * scale:
        return True
    if objective >= best_objective - TIE_REL_TOL * scale:
        return price < best_price
    return False


def _best_probability(segments: list[_Segment]) -> float:
    if not segments:
        return 0.0
    return max(
        max(segment.probability_at(segment.lo), segment.probability_at(segment.hi))
        for segment in segments
    )


def _worst_probability(segments: list[_Segment]) -> float:
    if not segments:  # pragma: no cover - callers guard
        return 0.0
    return min(
        min(segment.probability_at(segment.lo), segment.probability_at(segment.hi))
        for segment in segments
    )


def _price_conflict_reason(
    lower: float,
    upper: float,
    lower_sources: list[tuple[str, float]],
    upper_sources: list[tuple[str, float]],
    constraints: OptimizerConstraints,
    grid_lo: float,
    grid_hi: float,
) -> str:
    low_name = " and ".join(
        _bound_label(name, constraints)
        for name in _describe_bound(lower, lower_sources).split(" and ")
    )
    high_name = " and ".join(
        _bound_label(name, constraints)
        for name in _describe_bound(upper, upper_sources).split(" and ")
    )
    detail = ""
    if CONSTRAINT_MIN_MARGIN in low_name:
        detail = f" (margin measured on estimated_cost {_fmt(constraints.estimated_cost)})"
    elif CONSTRAINT_GRID_LOWER in low_name:
        detail = f" (the simulated price grid spans [{_fmt(grid_lo)}, {_fmt(grid_hi)}])"
    return (
        f"{low_name} requires a bid >= {_fmt(lower)} but {high_name} caps it at "
        f"{_fmt(upper)}{detail}"
    )


def _verify(
    price: float,
    *,
    constraints: OptimizerConstraints,
    margin_floor: float,
    hard_floor: float | None,
    upper: float,
    lower: float,
    target: float | None,
    probability: float,
) -> str | None:
    """Re-check a candidate bid against every constraint from scratch.

    Deliberately recomputes rather than reusing intermediate values: this is the
    gate that has to hold even if the geometry above is wrong.
    """
    if not math.isfinite(price) or price <= 0.0:
        return f"price {price} is not a positive finite number"
    if hard_floor is not None and price < hard_floor:
        return f"price {_fmt(price)} is below hard_cost_floor {_fmt(hard_floor)}"
    if price < margin_floor:
        return f"price {_fmt(price)} is below the margin floor {_fmt(margin_floor)}"
    achieved = margin_pct(price, constraints.estimated_cost)
    if achieved < float(constraints.min_margin_pct):
        return (
            f"margin {_fmt(achieved)}% is below min_margin_pct "
            f"{_fmt(constraints.min_margin_pct)}"
        )
    if constraints.max_bid is not None and price > float(constraints.max_bid):
        return f"price {_fmt(price)} exceeds max_bid {_fmt(constraints.max_bid)}"
    if price < lower or price > upper:
        return f"price {_fmt(price)} is outside the feasible band"
    if target is not None and probability < target:
        return (
            f"win probability {_fmt(probability)} is below target_win_probability "
            f"{_fmt(target)}"
        )
    return None


def _binding_constraints(
    price: float,
    *,
    probability: float,
    constraints: OptimizerConstraints,
    margin_floor: float,
    hard_floor: float | None,
    grid_lo: float,
    grid_hi: float,
    target: float | None,
) -> list[str]:
    """Which constraints the optimum sits on, in a stable order."""
    binding: list[str] = []
    if hard_floor is not None and _close(price, hard_floor):
        binding.append(CONSTRAINT_HARD_COST_FLOOR)
    if _close(price, margin_floor):
        binding.append(CONSTRAINT_MIN_MARGIN)
    if constraints.max_bid is not None and _close(price, float(constraints.max_bid)):
        binding.append(CONSTRAINT_MAX_BID)
    if target is not None and _close(probability, target):
        # The optimum sits exactly on the win-probability floor: pushing the
        # price any higher would drop below the target, so the target binds.
        binding.append(CONSTRAINT_TARGET_WIN_PROBABILITY)
    if _close(price, grid_lo) and CONSTRAINT_MIN_MARGIN not in binding:
        binding.append(CONSTRAINT_GRID_LOWER)
    if _close(price, grid_hi):
        binding.append(CONSTRAINT_GRID_UPPER)
    return binding


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=BOUNDARY_REL_TOL, abs_tol=BOUNDARY_REL_TOL)


def _range_for_threshold(
    feasible: list[_Segment], threshold: float, *, anchor: float | None
) -> tuple[float, float] | None:
    """Widest contiguous feasible interval whose win probability clears ``threshold``.

    Prefers the interval containing ``anchor`` (the recommended bid) so the band
    shown next to a recommendation actually contains it; otherwise the widest
    qualifying interval. Returns None when no feasible price clears the
    threshold — an absent band is an honest answer, an invented one is not.
    """
    qualifying = _feasible_segments(feasible, lo=-math.inf, hi=math.inf, target=threshold)
    intervals = _merge(qualifying)
    if not intervals:
        return None
    if anchor is not None:
        for lo, hi in intervals:
            if lo <= anchor <= hi:
                return (lo, hi)
    return max(intervals, key=lambda interval: (interval[1] - interval[0], -interval[0]))


def _aggressive_range(feasible: list[_Segment]) -> tuple[float, float] | None:
    """Feasible prices in the top decile of the attainable win-probability span.

    When the whole feasible set has one win probability (a flat curve) the top
    decile is the whole set, and that is what is returned — flat means price does
    not move the odds, and pretending to find an aggressive corner would be a
    fabrication.
    """
    if not feasible:
        return None
    best = _best_probability(feasible)
    worst = _worst_probability(feasible)
    threshold = best - AGGRESSIVE_TOP_DECILE * (best - worst)
    anchor = _argmax_price(feasible)
    return _range_for_threshold(feasible, threshold, anchor=anchor)


def _argmax_price(segments: list[_Segment]) -> float | None:
    """Feasible price with the highest win probability (lowest such price on a tie)."""
    best_price: float | None = None
    best_probability = -math.inf
    for segment in segments:
        for price in (segment.lo, segment.hi):
            probability = segment.probability_at(price)
            if probability > best_probability or (
                probability == best_probability and best_price is not None and price < best_price
            ):
                best_probability, best_price = probability, price
    return best_price


# --- Optional bridge to the shared prediction contract -----------------------

#: Explanation weights are reported on a 0-1 scale. The recommendation is driven
#: by the curve first and the cost inputs second; these two values are labels for
#: the UI ordering, not fitted coefficients, and are named so nobody mistakes
#: them for one.
WEIGHT_CURVE = 0.6
WEIGHT_COST = 0.4


def to_price_prediction(
    result: OptimizerResult,
    *,
    tender_id: int,
    constraints: OptimizerConstraints,
    evidence_count: int = 0,
    evidence_tier: EvidenceTier | None = None,
    median_evidence_age_days: float | None = None,
    confidence_score: int | None = None,
    similarity_confidence: int | None = None,
    seed: int | None = None,
    feature_snapshot_id: str | None = None,
    generated_at: datetime | None = None,
) -> PricePrediction:
    """Wrap an optimizer result in the shared `PricePrediction` contract.

    The band stored as p10/p50/p90 is a **recommendation band, not a sampling
    distribution**: p50 is the recommended bid, p10 the most aggressive
    margin-compliant price in the aggressive range and p90 the most conservative
    price in the safe range, each clamped so the ordering invariant holds. An
    explanation factor says so explicitly, because a reader who assumes these are
    simulation quantiles would over-read them.

    An infeasible result becomes a suppressed prediction with
    ``INSUFFICIENT_EVIDENCE`` unless the optimizer never got a usable curve, in
    which case ``MODEL_UNAVAILABLE`` is the honest reason.
    """
    scope = PredictionScope.USER_OPTIMIZER
    stamped = generated_at or datetime.now(UTC)
    freshness = (
        None if median_evidence_age_days is None else freshness_score(median_evidence_age_days)
    )

    if not result.is_feasible:
        return PricePrediction.suppressed(
            tender_id=tender_id,
            scope=scope,
            reason=SuppressionReason.INSUFFICIENT_EVIDENCE,
            evidence_count=evidence_count,
            evidence_tier=evidence_tier,
            data_freshness_score=freshness,
            seed=seed,
            feature_snapshot_id=feature_snapshot_id,
            generated_at=stamped,
            explanation_factors=[
                ExplanationFactor(
                    name="infeasible_constraints",
                    direction="neutral",
                    weight=1.0,
                    kind="derived",
                    detail=result.infeasible_reason or "no feasible bid",
                )
            ],
        )

    recommended = float(result.recommended_bid or 0.0)
    low = result.aggressive_range[0] if result.aggressive_range else recommended
    high = result.safe_range[1] if result.safe_range else recommended
    p10 = min(low, recommended)
    p90 = max(high, recommended)
    quantiles = Quantiles(p10=p10, p50=recommended, p90=p90).validate()

    factors = [
        ExplanationFactor(
            name="win_probability_curve",
            direction="neutral",
            weight=WEIGHT_CURVE,
            kind="predicted",
            detail=(
                f"simulated win probability at the recommended bid: "
                f"{_fmt(result.win_probability)}"
            ),
        ),
        ExplanationFactor(
            name="cost_and_margin_constraints",
            direction="increases",
            weight=WEIGHT_COST,
            kind="observed",
            detail=(
                f"estimated_cost {_fmt(constraints.estimated_cost)}, min_margin_pct "
                f"{_fmt(constraints.min_margin_pct)}, risk_reserve "
                f"{_fmt(constraints.risk_reserve)}"
            ),
        ),
        ExplanationFactor(
            name="band_semantics",
            direction="neutral",
            weight=0.0,
            kind="derived",
            detail=(
                "p10/p50/p90 are a recommendation band (aggressive / recommended / safe), "
                "not simulation quantiles"
            ),
        ),
    ]
    if result.binding_constraints:
        factors.append(
            ExplanationFactor(
                name="binding_constraints",
                direction="neutral",
                weight=0.0,
                kind="derived",
                detail=", ".join(result.binding_constraints),
            )
        )

    return PricePrediction(
        tender_id=tender_id,
        prediction_scope=scope,
        p10=quantiles.p10,
        p50=quantiles.p50,
        p90=quantiles.p90,
        expected_value=result.expected_contribution,
        win_probability=result.win_probability,
        confidence_score=confidence_score,
        similarity_confidence=similarity_confidence,
        data_freshness_score=freshness,
        evidence_count=evidence_count,
        evidence_tier=evidence_tier,
        model_version=MODEL_VERSION,
        feature_snapshot_id=feature_snapshot_id,
        seed=seed,
        generated_at=stamped,
        explanation_factors=factors,
    )


__all__ = [
    "AGGRESSIVE_TOP_DECILE",
    "BOUNDARY_REL_TOL",
    "CONSTRAINT_GRID_LOWER",
    "CONSTRAINT_GRID_UPPER",
    "CONSTRAINT_HARD_COST_FLOOR",
    "CONSTRAINT_MAX_BID",
    "CONSTRAINT_MIN_MARGIN",
    "CONSTRAINT_TARGET_WIN_PROBABILITY",
    "MAX_MARGIN_PCT",
    "SAFE_WIN_FLOOR",
    "OptimizerConstraints",
    "OptimizerError",
    "OptimizerResult",
    "margin_pct",
    "min_margin_price",
    "optimize",
    "to_price_prediction",
    "win_probability_at",
]
