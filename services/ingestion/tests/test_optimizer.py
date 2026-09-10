"""Tests for the P2W bid optimizer.

This is a red-gate module: "the optimizer can recommend below a hard cost or
minimum-margin constraint" is an automatic NO-GO for the product. The tests are
therefore weighted toward *adversarial* coverage of the feasible set — hundreds
of generated constraint combinations, an independent brute-force cross-check of
the argmax, and explicit checks that an empty feasible set produces a reason and
no bid rather than a nearby violating price.

Everything is deterministic: every random generator is seeded, there is no
sleeping, no network and no database access.
"""
from __future__ import annotations

import math
import random
from decimal import Decimal

import pytest

from thaqip_ingestion.p2w.contracts import (
    MODEL_VERSION,
    PredictionScope,
    SuppressionReason,
)
from thaqip_ingestion.p2w.optimizer import (
    AGGRESSIVE_TOP_DECILE,
    CONSTRAINT_GRID_LOWER,
    CONSTRAINT_GRID_UPPER,
    CONSTRAINT_HARD_COST_FLOOR,
    CONSTRAINT_MAX_BID,
    CONSTRAINT_MIN_MARGIN,
    CONSTRAINT_TARGET_WIN_PROBABILITY,
    SAFE_WIN_FLOOR,
    OptimizerConstraints,
    OptimizerError,
    OptimizerResult,
    margin_pct,
    min_margin_price,
    optimize,
    to_price_prediction,
    win_probability_at,
)

# --- Independent reference implementations (deliberately NOT reusing internals) --


def reference_probability(pairs: list[tuple[float, float]], price: float) -> float:
    """Piecewise-linear interpolation, written independently of the module."""
    if price <= pairs[0][0]:
        return pairs[0][1]
    if price >= pairs[-1][0]:
        return pairs[-1][1]
    for index in range(1, len(pairs)):
        x0, p0 = pairs[index - 1]
        x1, p1 = pairs[index]
        if x0 <= price <= x1:
            weight = (price - x0) / (x1 - x0)
            return p0 + weight * (p1 - p0)
    raise AssertionError("unreachable")  # pragma: no cover


def reference_bounds(
    pairs: list[tuple[float, float]], constraints: OptimizerConstraints
) -> tuple[float, float]:
    cost = constraints.estimated_cost
    margin_floor = cost / (1.0 - constraints.min_margin_pct / 100.0)
    lower = max(margin_floor, constraints.hard_cost_floor or 0.0, pairs[0][0])
    upper = pairs[-1][0]
    if constraints.max_bid is not None:
        upper = min(upper, constraints.max_bid)
    return lower, upper


def brute_force_best(
    pairs: list[tuple[float, float]],
    constraints: OptimizerConstraints,
    *,
    steps: int = 20_001,
) -> tuple[float | None, float]:
    """Exhaustive scan of the feasible set. Returns (best price, best objective)."""
    lower, upper = reference_bounds(pairs, constraints)
    if lower > upper:
        return None, -math.inf
    base = constraints.estimated_cost + constraints.risk_reserve
    best_price: float | None = None
    best_objective = -math.inf
    for step in range(steps):
        price = lower + (upper - lower) * step / (steps - 1)
        if price <= 0:
            continue
        if (price - constraints.estimated_cost) / price * 100.0 < constraints.min_margin_pct:
            continue
        probability = reference_probability(pairs, price)
        if (
            constraints.target_win_probability is not None
            and probability < constraints.target_win_probability
        ):
            continue
        objective = probability * (price - base)
        if objective > best_objective:
            best_objective, best_price = objective, price
    return best_price, best_objective


def monotone_curve(rng: random.Random, *, points: int = 24, low: float = 500_000.0) -> list[dict]:
    """A non-increasing win-probability curve at realistic Saudi tender magnitudes.

    Shaped like the montecarlo output it stands in for: probabilities start high
    at the cheap end and decay, exactly monotone because `win_probability_curve`
    uses common random numbers.
    """
    price = low * rng.uniform(0.6, 1.4)
    step = price * rng.uniform(0.01, 0.06)
    probability = rng.uniform(0.75, 1.0)
    curve = []
    for _ in range(points):
        curve.append({"price": price, "win_probability": max(0.0, min(1.0, probability))})
        price += step * rng.uniform(0.5, 1.5)
        probability -= rng.uniform(0.0, 0.14)
    return curve


def pairs_of(curve: list[dict]) -> list[tuple[float, float]]:
    return sorted((point["price"], point["win_probability"]) for point in curve)


LINEAR_CURVE = [
    {"price": 800.0 + 50.0 * index, "win_probability": max(0.0, 1.0 - (50.0 * index) / 600.0)}
    for index in range(16)
]

# --- margin_pct --------------------------------------------------------------


def test_margin_pct_is_revenue_margin() -> None:
    assert margin_pct(1000.0, 800.0) == pytest.approx(20.0)
    assert margin_pct(1000.0, 1000.0) == pytest.approx(0.0)
    assert margin_pct(1000.0, 1200.0) == pytest.approx(-20.0)
    assert margin_pct(1000.0, 0.0) == pytest.approx(100.0)


@pytest.mark.parametrize("bid", [0.0, -1.0, float("nan"), float("inf"), None, "900"])
def test_margin_pct_rejects_unusable_bids(bid: object) -> None:
    with pytest.raises(OptimizerError):
        margin_pct(bid, 100.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("cost", [float("nan"), None, -5.0, "800"])
def test_margin_pct_rejects_unusable_costs(cost: object) -> None:
    with pytest.raises(OptimizerError):
        margin_pct(1000.0, cost)  # type: ignore[arg-type]


def test_margin_pct_accepts_decimal_because_asyncpg_returns_numeric_as_decimal() -> None:
    assert margin_pct(Decimal(1000), Decimal(800)) == pytest.approx(20.0)


def test_margin_pct_rejects_bool_which_python_would_otherwise_accept() -> None:
    with pytest.raises(OptimizerError):
        margin_pct(True, 0.0)  # type: ignore[arg-type]


# --- min_margin_price --------------------------------------------------------


def test_min_margin_price_closed_form() -> None:
    assert min_margin_price(800.0, 20.0) == pytest.approx(1000.0)
    assert min_margin_price(0.0, 30.0) == 0.0
    assert min_margin_price(800.0, 100.0) is None
    assert min_margin_price(800.0, 250.0) is None
    assert min_margin_price(0.0, 100.0) == 0.0


def test_min_margin_price_is_floating_point_safe_over_many_inputs() -> None:
    """The returned floor must itself pass margin_pct, not merely satisfy algebra."""
    rng = random.Random(20260910)
    for _ in range(2000):
        cost = rng.uniform(1.0, 5_000_000.0)
        margin = rng.uniform(-40.0, 99.5)
        floor = min_margin_price(cost, margin)
        assert floor is not None
        if floor > 0:
            assert margin_pct(floor, cost) >= margin


# --- input validation --------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"estimated_cost": float("nan"), "min_margin_pct": 10.0},
        {"estimated_cost": None, "min_margin_pct": 10.0},
        {"estimated_cost": float("inf"), "min_margin_pct": 10.0},
        {"estimated_cost": -1.0, "min_margin_pct": 10.0},
        {"estimated_cost": 100.0, "min_margin_pct": float("nan")},
        {"estimated_cost": 100.0, "min_margin_pct": None},
        {"estimated_cost": 100.0, "min_margin_pct": 10.0, "risk_reserve": -1.0},
        {"estimated_cost": 100.0, "min_margin_pct": 10.0, "risk_reserve": float("nan")},
        {"estimated_cost": 100.0, "min_margin_pct": 10.0, "hard_cost_floor": -5.0},
        {"estimated_cost": 100.0, "min_margin_pct": 10.0, "target_win_probability": 1.5},
        {"estimated_cost": 100.0, "min_margin_pct": 10.0, "target_win_probability": -0.1},
        {"estimated_cost": 100.0, "min_margin_pct": 10.0, "max_bid": 0.0},
        {"estimated_cost": 100.0, "min_margin_pct": 10.0, "max_bid": float("nan")},
    ],
)
def test_constraints_reject_unusable_inputs(kwargs: dict) -> None:
    with pytest.raises(OptimizerError):
        OptimizerConstraints(**kwargs)


def test_nan_cost_disables_the_optimizer_without_crashing() -> None:
    """A NaN cost must never reach the objective function."""
    with pytest.raises(OptimizerError) as excinfo:
        OptimizerConstraints(estimated_cost=float("nan"), min_margin_pct=10.0)
    assert "estimated_cost" in str(excinfo.value)


def test_mutated_constraints_are_revalidated_by_optimize() -> None:
    constraints = OptimizerConstraints(estimated_cost=800.0, min_margin_pct=10.0)
    constraints.estimated_cost = float("nan")  # dataclass is mutable; trust nothing
    with pytest.raises(OptimizerError):
        optimize(curve=LINEAR_CURVE, constraints=constraints)


def test_optimize_rejects_a_non_constraints_object() -> None:
    with pytest.raises(OptimizerError):
        optimize(curve=LINEAR_CURVE, constraints={"estimated_cost": 1.0})  # type: ignore[arg-type]


# --- curve handling ----------------------------------------------------------


def test_curve_accepts_mappings_tuples_and_objects_alike() -> None:
    class Point:
        def __init__(self, price: float, win_probability: float) -> None:
            self.price = price
            self.win_probability = win_probability

    constraints = OptimizerConstraints(estimated_cost=800.0, min_margin_pct=5.0)
    as_mapping = optimize(curve=LINEAR_CURVE, constraints=constraints)
    as_tuples = optimize(
        curve=[(p["price"], p["win_probability"]) for p in LINEAR_CURVE], constraints=constraints
    )
    as_objects = optimize(
        curve=[Point(p["price"], p["win_probability"]) for p in LINEAR_CURVE],
        constraints=constraints,
    )
    assert as_mapping.recommended_bid == as_tuples.recommended_bid == as_objects.recommended_bid


def test_curve_rejects_empty_conflicting_and_malformed_entries() -> None:
    constraints = OptimizerConstraints(estimated_cost=800.0, min_margin_pct=5.0)
    with pytest.raises(OptimizerError):
        optimize(curve=[], constraints=constraints)
    with pytest.raises(OptimizerError):
        optimize(
            curve=[
                {"price": 900.0, "win_probability": 0.5},
                {"price": 900.0, "win_probability": 0.4},
            ],
            constraints=constraints,
        )
    with pytest.raises(OptimizerError):
        optimize(curve=[{"price": 900.0}], constraints=constraints)
    with pytest.raises(OptimizerError):
        optimize(curve=[object()], constraints=constraints)
    with pytest.raises(OptimizerError):
        optimize(curve=[(900.0, 1.4)], constraints=constraints)
    with pytest.raises(OptimizerError):
        optimize(curve=[(float("nan"), 0.4)], constraints=constraints)


def test_duplicate_but_consistent_curve_points_are_collapsed() -> None:
    constraints = OptimizerConstraints(estimated_cost=800.0, min_margin_pct=5.0)
    doubled = LINEAR_CURVE + LINEAR_CURVE
    assert optimize(curve=doubled, constraints=constraints).recommended_bid == pytest.approx(
        optimize(curve=LINEAR_CURVE, constraints=constraints).recommended_bid
    )


def test_win_probability_at_interpolates_and_refuses_to_extrapolate() -> None:
    assert win_probability_at(LINEAR_CURVE, 800.0) == pytest.approx(1.0)
    assert win_probability_at(LINEAR_CURVE, 825.0) == pytest.approx(1.0 - 25.0 / 600.0)
    with pytest.raises(OptimizerError):
        win_probability_at(LINEAR_CURVE, 799.0)
    with pytest.raises(OptimizerError):
        win_probability_at(LINEAR_CURVE, 10_000.0)


def test_single_point_curve_is_usable() -> None:
    constraints = OptimizerConstraints(estimated_cost=800.0, min_margin_pct=5.0)
    result = optimize(curve=[{"price": 1000.0, "win_probability": 0.4}], constraints=constraints)
    assert result.recommended_bid == pytest.approx(1000.0)
    assert result.win_probability == pytest.approx(0.4)
    assert CONSTRAINT_GRID_LOWER in result.binding_constraints
    assert CONSTRAINT_GRID_UPPER in result.binding_constraints


# --- brute-force cross-check -------------------------------------------------


def test_optimum_matches_an_exhaustive_scan_on_generated_curves() -> None:
    """The closed-form optimum must beat (never lose to) a 20k-point scan."""
    rng = random.Random(4242)
    checked = 0
    for _ in range(60):
        curve = monotone_curve(rng)
        pairs = pairs_of(curve)
        cost = pairs[0][0] * rng.uniform(0.3, 0.95)
        constraints = OptimizerConstraints(
            estimated_cost=cost,
            min_margin_pct=rng.uniform(0.0, 15.0),
            risk_reserve=cost * rng.uniform(0.0, 0.05),
        )
        result = optimize(curve=curve, constraints=constraints)
        scan_price, scan_objective = brute_force_best(pairs, constraints)
        if result.recommended_bid is None:
            assert scan_price is None
            continue
        checked += 1
        scale = max(abs(scan_objective), 1.0)
        # The optimizer is exact for the interpolated curve, so it can only be
        # better than a finite scan; it may never be meaningfully worse.
        assert result.expected_contribution >= scan_objective - 1e-9 * scale
        assert result.expected_contribution <= scan_objective + 1e-3 * scale
        assert result.recommended_bid == pytest.approx(scan_price, rel=1e-3, abs=1e-3)
    assert checked >= 50, "the cross-check must actually exercise feasible cases"


def test_optimum_matches_an_exhaustive_scan_with_every_constraint_active() -> None:
    rng = random.Random(90210)
    feasible_seen = 0
    infeasible_seen = 0
    for _ in range(120):
        curve = monotone_curve(rng)
        pairs = pairs_of(curve)
        cost = pairs[0][0] * rng.uniform(0.3, 1.1)
        constraints = OptimizerConstraints(
            estimated_cost=cost,
            min_margin_pct=rng.uniform(0.0, 25.0),
            hard_cost_floor=cost * rng.uniform(0.8, 1.3),
            target_win_probability=rng.choice([None, rng.uniform(0.05, 0.6)]),
            risk_reserve=cost * rng.uniform(0.0, 0.05),
            max_bid=rng.choice([None, pairs[-1][0] * rng.uniform(0.5, 1.2)]),
        )
        result = optimize(curve=curve, constraints=constraints)
        _, scan_objective = brute_force_best(pairs, constraints)
        if result.recommended_bid is None:
            infeasible_seen += 1
            continue
        feasible_seen += 1
        scale = max(abs(scan_objective), 1.0)
        assert result.expected_contribution >= scan_objective - 1e-6 * scale
    assert feasible_seen >= 20
    assert infeasible_seen >= 1


# --- hard constraint enforcement (the red gate) ------------------------------


def test_hard_constraints_are_never_violated_across_many_combinations() -> None:
    rng = random.Random(777_777)
    feasible = 0
    infeasible = 0
    for _ in range(400):
        curve = monotone_curve(rng, points=rng.randint(2, 30))
        pairs = pairs_of(curve)
        cost = pairs[0][0] * rng.uniform(0.2, 1.6)
        constraints = OptimizerConstraints(
            estimated_cost=cost,
            min_margin_pct=rng.uniform(-10.0, 60.0),
            hard_cost_floor=rng.choice([None, cost * rng.uniform(0.5, 2.0)]),
            target_win_probability=rng.choice([None, rng.uniform(0.0, 0.99)]),
            risk_reserve=cost * rng.uniform(0.0, 0.2),
            max_bid=rng.choice([None, pairs[-1][0] * rng.uniform(0.3, 1.5)]),
        )
        result = optimize(curve=curve, constraints=constraints)
        if result.recommended_bid is None:
            infeasible += 1
            assert result.infeasible_reason
            assert result.expected_contribution is None
            assert result.win_probability is None
            assert result.margin_pct is None
            assert result.safe_range is None
            assert result.aggressive_range is None
            continue
        feasible += 1
        bid = result.recommended_bid
        # Every hard constraint, re-checked from the raw inputs.
        assert bid > 0
        if constraints.hard_cost_floor is not None:
            assert bid >= constraints.hard_cost_floor
        assert margin_pct(bid, constraints.estimated_cost) >= constraints.min_margin_pct
        if constraints.max_bid is not None:
            assert bid <= constraints.max_bid
        if constraints.target_win_probability is not None:
            assert result.win_probability >= constraints.target_win_probability
        assert pairs[0][0] <= bid <= pairs[-1][0]
    assert feasible >= 100
    assert infeasible >= 20


def test_bid_never_falls_below_a_hard_floor_that_sits_between_grid_points() -> None:
    """The dangerous case: the floor is not one of the simulated prices."""
    curve = [{"price": 1000.0 + 100.0 * i, "win_probability": 1.0 - 0.1 * i} for i in range(10)]
    for floor in (1000.01, 1049.999, 1150.5, 1799.9999):
        constraints = OptimizerConstraints(
            estimated_cost=900.0, min_margin_pct=0.0, hard_cost_floor=floor
        )
        result = optimize(curve=curve, constraints=constraints)
        assert result.recommended_bid is not None
        assert result.recommended_bid >= floor


def test_bid_never_falls_below_a_margin_floor_that_sits_between_grid_points() -> None:
    curve = [{"price": 1000.0 + 100.0 * i, "win_probability": 1.0 - 0.1 * i} for i in range(10)]
    rng = random.Random(31337)
    for _ in range(300):
        cost = rng.uniform(700.0, 1500.0)
        margin = rng.uniform(0.0, 35.0)
        constraints = OptimizerConstraints(estimated_cost=cost, min_margin_pct=margin)
        result = optimize(curve=curve, constraints=constraints)
        if result.recommended_bid is None:
            continue
        assert margin_pct(result.recommended_bid, cost) >= margin


def test_zero_margin_zero_cost_is_still_a_positive_price() -> None:
    constraints = OptimizerConstraints(estimated_cost=0.0, min_margin_pct=0.0)
    result = optimize(curve=LINEAR_CURVE, constraints=constraints)
    assert result.recommended_bid is not None
    assert result.recommended_bid > 0


# --- infeasibility -----------------------------------------------------------


def test_unreachable_target_names_the_conflicting_constraints() -> None:
    constraints = OptimizerConstraints(
        estimated_cost=800.0, min_margin_pct=15.0, target_win_probability=0.8
    )
    result = optimize(curve=LINEAR_CURVE, constraints=constraints)
    assert result.recommended_bid is None
    reason = result.infeasible_reason or ""
    assert "target_win_probability 0.8" in reason
    assert "min_margin_pct 15" in reason
    assert "unreachable" in reason


def test_margin_above_the_grid_reports_the_price_conflict() -> None:
    constraints = OptimizerConstraints(estimated_cost=800.0, min_margin_pct=60.0)
    result = optimize(curve=LINEAR_CURVE, constraints=constraints)
    assert result.recommended_bid is None
    reason = result.infeasible_reason or ""
    assert "min_margin_pct 60" in reason
    assert "2000" in reason  # 800 / (1 - 0.60)
    assert CONSTRAINT_GRID_UPPER in reason


def test_max_bid_below_the_hard_floor_reports_both_sides() -> None:
    constraints = OptimizerConstraints(
        estimated_cost=800.0, min_margin_pct=0.0, hard_cost_floor=1400.0, max_bid=1000.0
    )
    result = optimize(curve=LINEAR_CURVE, constraints=constraints)
    assert result.recommended_bid is None
    reason = result.infeasible_reason or ""
    assert CONSTRAINT_HARD_COST_FLOOR in reason
    assert CONSTRAINT_MAX_BID in reason


def test_margin_of_one_hundred_percent_is_unreachable_not_infinite() -> None:
    constraints = OptimizerConstraints(estimated_cost=800.0, min_margin_pct=100.0)
    result = optimize(curve=LINEAR_CURVE, constraints=constraints)
    assert result.recommended_bid is None
    assert "unreachable" in (result.infeasible_reason or "")


def test_max_bid_below_the_grid_is_infeasible_not_clamped() -> None:
    constraints = OptimizerConstraints(
        estimated_cost=100.0, min_margin_pct=0.0, max_bid=500.0
    )
    result = optimize(curve=LINEAR_CURVE, constraints=constraints)
    assert result.recommended_bid is None
    assert CONSTRAINT_MAX_BID in (result.infeasible_reason or "")


def test_result_state_machine_forbids_half_answers() -> None:
    with pytest.raises(OptimizerError):
        OptimizerResult()
    with pytest.raises(OptimizerError):
        OptimizerResult(recommended_bid=1000.0, infeasible_reason="nope")


# --- boundary optima and binding constraints ---------------------------------


def test_margin_floor_boundary_optimum_is_reported() -> None:
    """A steep curve pushes the optimum onto the margin floor, which is legal."""
    curve = [{"price": 1000.0 + 100.0 * i, "win_probability": max(0.0, 1.0 - 0.5 * i)} for i in
             range(6)]
    constraints = OptimizerConstraints(estimated_cost=900.0, min_margin_pct=20.0)
    result = optimize(curve=curve, constraints=constraints)
    assert result.recommended_bid == pytest.approx(1125.0, rel=1e-9)
    assert CONSTRAINT_MIN_MARGIN in result.binding_constraints
    assert margin_pct(result.recommended_bid, 900.0) >= 20.0


def test_hard_floor_boundary_optimum_is_reported() -> None:
    curve = [{"price": 1000.0 + 100.0 * i, "win_probability": max(0.0, 1.0 - 0.5 * i)} for i in
             range(6)]
    constraints = OptimizerConstraints(
        estimated_cost=500.0, min_margin_pct=0.0, hard_cost_floor=1150.0
    )
    result = optimize(curve=curve, constraints=constraints)
    assert result.recommended_bid == pytest.approx(1150.0)
    assert CONSTRAINT_HARD_COST_FLOOR in result.binding_constraints


def test_max_bid_boundary_optimum_is_reported() -> None:
    """A shallow curve pushes the optimum up until max_bid stops it."""
    curve = [{"price": 1000.0 + 100.0 * i, "win_probability": 0.9 - 0.005 * i} for i in range(6)]
    constraints = OptimizerConstraints(
        estimated_cost=100.0, min_margin_pct=0.0, max_bid=1250.0
    )
    result = optimize(curve=curve, constraints=constraints)
    assert result.recommended_bid == pytest.approx(1250.0)
    assert CONSTRAINT_MAX_BID in result.binding_constraints


def test_grid_upper_bound_censoring_is_reported() -> None:
    curve = [{"price": 1000.0 + 100.0 * i, "win_probability": 0.9 - 0.005 * i} for i in range(6)]
    constraints = OptimizerConstraints(estimated_cost=100.0, min_margin_pct=0.0)
    result = optimize(curve=curve, constraints=constraints)
    assert result.recommended_bid == pytest.approx(1500.0)
    assert CONSTRAINT_GRID_UPPER in result.binding_constraints


def test_target_win_probability_boundary_optimum_is_reported() -> None:
    constraints = OptimizerConstraints(
        estimated_cost=800.0, min_margin_pct=0.0, target_win_probability=0.75
    )
    result = optimize(curve=LINEAR_CURVE, constraints=constraints)
    assert result.recommended_bid is not None
    assert result.win_probability >= 0.75
    assert CONSTRAINT_TARGET_WIN_PROBABILITY in result.binding_constraints
    assert result.recommended_bid == pytest.approx(950.0)  # 1 - 150/600 = 0.75


def test_interior_optimum_has_no_binding_constraints() -> None:
    constraints = OptimizerConstraints(estimated_cost=800.0, min_margin_pct=1.0)
    result = optimize(curve=LINEAR_CURVE, constraints=constraints)
    assert result.recommended_bid == pytest.approx(1100.0)
    assert result.binding_constraints == []


# --- directional sanity ------------------------------------------------------


def test_raising_estimated_cost_never_lowers_the_recommended_bid() -> None:
    """Comparative statics: d(argmax)/d(cost) >= 0 for a non-increasing curve."""
    rng = random.Random(11_235)
    series_checked = 0
    for _ in range(40):
        curve = monotone_curve(rng)
        pairs = pairs_of(curve)
        previous: float | None = None
        steps = 0
        for fraction in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1]:
            constraints = OptimizerConstraints(
                estimated_cost=pairs[0][0] * fraction, min_margin_pct=5.0
            )
            result = optimize(curve=curve, constraints=constraints)
            if result.recommended_bid is None:
                continue
            if previous is not None:
                assert result.recommended_bid >= previous - 1e-6 * max(previous, 1.0)
                steps += 1
            previous = result.recommended_bid
        if steps:
            series_checked += 1
    assert series_checked >= 30


def test_raising_the_risk_reserve_never_lowers_the_recommended_bid() -> None:
    rng = random.Random(5_813)
    for _ in range(20):
        curve = monotone_curve(rng)
        pairs = pairs_of(curve)
        cost = pairs[0][0] * 0.5
        previous: float | None = None
        for reserve_fraction in [0.0, 0.05, 0.1, 0.2, 0.3]:
            constraints = OptimizerConstraints(
                estimated_cost=cost,
                min_margin_pct=0.0,
                risk_reserve=cost * reserve_fraction,
            )
            result = optimize(curve=curve, constraints=constraints)
            assert result.recommended_bid is not None
            if previous is not None:
                assert result.recommended_bid >= previous - 1e-6 * max(previous, 1.0)
            previous = result.recommended_bid


def test_tightening_the_target_never_raises_the_recommended_bid() -> None:
    previous: float | None = None
    for target in [0.0, 0.2, 0.4, 0.6, 0.8]:
        constraints = OptimizerConstraints(
            estimated_cost=700.0, min_margin_pct=0.0, target_win_probability=target
        )
        result = optimize(curve=LINEAR_CURVE, constraints=constraints)
        assert result.recommended_bid is not None
        if previous is not None:
            assert result.recommended_bid <= previous + 1e-9
        previous = result.recommended_bid


# --- ranges ------------------------------------------------------------------


def test_safe_range_is_feasible_and_clears_the_safe_win_floor() -> None:
    rng = random.Random(24_680)
    with_range = 0
    for _ in range(80):
        curve = monotone_curve(rng)
        pairs = pairs_of(curve)
        cost = pairs[0][0] * rng.uniform(0.2, 0.8)
        constraints = OptimizerConstraints(
            estimated_cost=cost, min_margin_pct=rng.uniform(0.0, 12.0)
        )
        result = optimize(curve=curve, constraints=constraints)
        if result.recommended_bid is None or result.safe_range is None:
            continue
        with_range += 1
        low, high = result.safe_range
        assert low <= high
        for price in (low, high, (low + high) / 2):
            assert margin_pct(price, cost) >= constraints.min_margin_pct
            assert win_probability_at(curve, price) >= SAFE_WIN_FLOOR - 1e-9
    assert with_range >= 40


def test_aggressive_range_sits_in_the_top_win_probability_decile() -> None:
    rng = random.Random(13_579)
    checked = 0
    for _ in range(80):
        curve = monotone_curve(rng)
        pairs = pairs_of(curve)
        cost = pairs[0][0] * rng.uniform(0.2, 0.8)
        constraints = OptimizerConstraints(
            estimated_cost=cost, min_margin_pct=rng.uniform(0.0, 12.0)
        )
        result = optimize(curve=curve, constraints=constraints)
        if result.recommended_bid is None or result.aggressive_range is None:
            continue
        checked += 1
        low, high = result.aggressive_range
        lower_bound, upper_bound = reference_bounds(pairs, constraints)
        assert lower_bound - 1e-6 <= low <= high <= upper_bound + 1e-6
        best = reference_probability(pairs, lower_bound)
        worst = reference_probability(pairs, upper_bound)
        threshold = best - AGGRESSIVE_TOP_DECILE * (best - worst)
        assert reference_probability(pairs, high) >= threshold - 1e-9
        assert margin_pct(low, cost) >= constraints.min_margin_pct
    assert checked >= 40


def test_safe_range_is_absent_when_nothing_feasible_is_safe() -> None:
    """Suppressing the band is the correct answer, not widening it."""
    curve = [{"price": 1000.0 + 100.0 * i, "win_probability": 0.2 - 0.02 * i} for i in range(6)]
    constraints = OptimizerConstraints(estimated_cost=500.0, min_margin_pct=0.0)
    result = optimize(curve=curve, constraints=constraints)
    assert result.recommended_bid is not None
    assert result.safe_range is None
    assert result.aggressive_range is not None


def test_flat_curve_gives_the_whole_feasible_set_as_the_aggressive_range() -> None:
    curve = [{"price": 1000.0 + 100.0 * i, "win_probability": 0.5} for i in range(6)]
    constraints = OptimizerConstraints(estimated_cost=500.0, min_margin_pct=0.0)
    result = optimize(curve=curve, constraints=constraints)
    assert result.aggressive_range == (1000.0, 1500.0)
    # Flat curve: price does not move the odds, so take the highest price.
    assert result.recommended_bid == pytest.approx(1500.0)


# --- non-monotone and degenerate curves --------------------------------------


def test_non_monotone_curve_yields_a_feasible_bid_and_a_correct_argmax() -> None:
    """A simulated curve can wobble; the union of feasible intervals must be right."""
    curve = [
        {"price": 1000.0, "win_probability": 0.9},
        {"price": 1100.0, "win_probability": 0.3},
        {"price": 1200.0, "win_probability": 0.85},
        {"price": 1300.0, "win_probability": 0.2},
    ]
    pairs = pairs_of(curve)
    constraints = OptimizerConstraints(
        estimated_cost=800.0, min_margin_pct=0.0, target_win_probability=0.5
    )
    result = optimize(curve=curve, constraints=constraints)
    assert result.recommended_bid is not None
    assert result.win_probability >= 0.5 - 1e-12
    _, scan_objective = brute_force_best(pairs, constraints)
    assert result.expected_contribution >= scan_objective - 1e-6 * max(abs(scan_objective), 1.0)


def test_negative_expected_contribution_is_reported_not_hidden() -> None:
    """A large risk reserve can make every feasible bid loss-making. Say so."""
    curve = [{"price": 1000.0 + 10.0 * i, "win_probability": 0.5} for i in range(6)]
    constraints = OptimizerConstraints(
        estimated_cost=900.0, min_margin_pct=0.0, risk_reserve=500.0, max_bid=1050.0
    )
    result = optimize(curve=curve, constraints=constraints)
    assert result.recommended_bid is not None
    assert result.expected_contribution < 0
    assert result.margin_pct > 0


def test_realistic_saudi_magnitudes_stay_numerically_sane() -> None:
    """Grounded in real offer spreads: tender 1692 ran 388,800 - 2,186,784 SAR."""
    curve = [
        {"price": price, "win_probability": probability}
        for price, probability in [
            (388_800.0, 0.98),
            (700_000.0, 0.82),
            (1_000_618.0, 0.51),
            (1_500_000.0, 0.18),
            (2_186_784.0, 0.02),
        ]
    ]
    constraints = OptimizerConstraints(
        estimated_cost=620_000.0, min_margin_pct=8.0, risk_reserve=25_000.0
    )
    result = optimize(curve=curve, constraints=constraints)
    assert result.recommended_bid is not None
    assert margin_pct(result.recommended_bid, 620_000.0) >= 8.0
    assert 388_800.0 <= result.recommended_bid <= 2_186_784.0
    _, scan_objective = brute_force_best(pairs_of(curve), constraints)
    assert result.expected_contribution >= scan_objective - 1e-6 * abs(scan_objective)


# --- determinism -------------------------------------------------------------


def test_repeated_calls_are_bit_identical() -> None:
    rng = random.Random(999)
    for _ in range(20):
        curve = monotone_curve(rng)
        constraints = OptimizerConstraints(
            estimated_cost=curve[0]["price"] * 0.6,
            min_margin_pct=7.5,
            risk_reserve=1000.0,
        )
        first = optimize(curve=curve, constraints=constraints)
        second = optimize(curve=list(reversed(curve)), constraints=constraints)
        assert first.to_dict() == second.to_dict()


# --- integration with the montecarlo curve -----------------------------------


def test_optimizes_a_real_montecarlo_curve() -> None:
    montecarlo = pytest.importorskip("thaqip_ingestion.p2w.montecarlo")
    competitors = [
        montecarlo.CompetitorDraw(
            vendor_id=1,
            participation_p=0.9,
            log_mu=math.log(1_000_000.0),
            log_sigma=0.15,
            technical_pass_p=0.9,
        ),
        montecarlo.CompetitorDraw(
            vendor_id=2,
            participation_p=0.7,
            log_mu=math.log(1_150_000.0),
            log_sigma=0.20,
            technical_pass_p=0.85,
        ),
    ]
    grid = [700_000.0 + 50_000.0 * index for index in range(15)]
    curve = montecarlo.win_probability_curve(
        price_grid=grid, competitors=competitors, seed=7, iterations=2000
    )
    constraints = OptimizerConstraints(
        estimated_cost=650_000.0, min_margin_pct=10.0, risk_reserve=10_000.0
    )
    result = optimize(curve=curve, constraints=constraints)
    assert result.recommended_bid is not None
    assert margin_pct(result.recommended_bid, 650_000.0) >= 10.0
    _, scan_objective = brute_force_best(
        [(point.price, point.win_probability) for point in curve], constraints
    )
    assert result.expected_contribution >= scan_objective - 1e-6 * max(abs(scan_objective), 1.0)


# --- bridge to the shared prediction contract --------------------------------


def test_feasible_result_becomes_an_ordered_user_optimizer_prediction() -> None:
    constraints = OptimizerConstraints(estimated_cost=800.0, min_margin_pct=5.0)
    result = optimize(curve=LINEAR_CURVE, constraints=constraints)
    prediction = to_price_prediction(
        result, tender_id=1692, constraints=constraints, evidence_count=23, seed=7
    )
    assert prediction.prediction_scope is PredictionScope.USER_OPTIMIZER
    assert prediction.model_version == MODEL_VERSION
    assert prediction.p10 <= prediction.p50 <= prediction.p90
    assert prediction.p50 == pytest.approx(result.recommended_bid)
    assert not prediction.is_suppressed
    kinds = {factor.kind for factor in prediction.explanation_factors}
    assert kinds == {"observed", "derived", "predicted"}
    assert any("not simulation quantiles" in f.detail for f in prediction.explanation_factors)


def test_infeasible_result_becomes_a_suppressed_prediction() -> None:
    constraints = OptimizerConstraints(estimated_cost=800.0, min_margin_pct=60.0)
    result = optimize(curve=LINEAR_CURVE, constraints=constraints)
    prediction = to_price_prediction(
        result, tender_id=1692, constraints=constraints, median_evidence_age_days=400.0
    )
    assert prediction.is_suppressed
    assert prediction.suppression_reason is SuppressionReason.INSUFFICIENT_EVIDENCE
    assert prediction.p10 is None and prediction.p50 is None and prediction.p90 is None
    assert prediction.data_freshness_score is not None
    assert prediction.explanation_factors[0].detail == result.infeasible_reason
