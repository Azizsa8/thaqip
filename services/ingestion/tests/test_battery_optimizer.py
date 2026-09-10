"""OPTIMIZER RED-GATE BATTERY (Testing guide section 13).

"The optimizer can recommend below a hard cost floor or below the minimum
margin" is an automatic NO-GO for this product. This file exists to try hard to
MAKE THAT HAPPEN and then prove it cannot:

* a deterministic property sweep over 500+ (curve shape x cost x min_margin x
  hard_floor x target_win x max_bid x risk_reserve) combinations, asserting the
  three hard invariants on every returned recommendation;
* an independent brute-force scan of the price grid that must agree with the
  closed-form argmax within tolerance, and must never find a feasible price when
  the optimizer claimed infeasibility;
* explicit conflicting-constraint cases that must produce recommended_bid=None
  plus a named infeasible_reason;
* boundary optima reported in binding_constraints;
* directional monotonicity (more cost / more required margin never lowers the
  recommended bid);
* degenerate inputs (empty curve, single point, None/NaN/inf/negative cost,
  zero margin, bool, string) - typed OptimizerError or an explicit infeasible
  state, never a crash and never a number.

Everything here is pure, seeded and offline. The one live-API test is marked
``live_api`` and skips itself when the console on localhost:8091 is not up.

The floating-point corner in ``min_margin_price`` that this battery found (see
test_margin_floor_converges_for_extreme_margins) is the reason the sweep grids
include margins in the 99.9%+ band: a red-gate module must not raise an internal
error on inputs the API layer accepts.
"""
from __future__ import annotations

import itertools
import json
import math
import random
import urllib.error
import urllib.request

import pytest
from _live_auth import auth_headers

from thaqip_ingestion.p2w.optimizer import (
    CONSTRAINT_GRID_LOWER,
    CONSTRAINT_GRID_UPPER,
    CONSTRAINT_HARD_COST_FLOOR,
    CONSTRAINT_MAX_BID,
    CONSTRAINT_MIN_MARGIN,
    CONSTRAINT_TARGET_WIN_PROBABILITY,
    MAX_MARGIN_PCT,
    OptimizerConstraints,
    OptimizerError,
    OptimizerResult,
    margin_pct,
    min_margin_price,
    optimize,
    to_price_prediction,
    win_probability_at,
)

CONSOLE_BASE = "http://localhost:8091"

# Comparisons against the user's own constraints are EXACT. A red-gate check
# that grants itself an epsilon is not a red-gate check; the module is expected
# to have already nudged its own floors into floating-point compliance.
EXACT = 0.0

# --- curve builders ----------------------------------------------------------


def _curve(prices: list[float], probabilities: list[float]) -> list[tuple[float, float]]:
    return list(zip(prices, probabilities, strict=True))


def _shape(name: str, n: int, rng: random.Random) -> list[float]:
    """Win probabilities for ``n`` grid points, by shape name."""
    if n == 1:
        return [0.5]
    ts = [i / (n - 1) for i in range(n)]
    if name == "monotone":
        return [1.0 - t for t in ts]
    if name == "steep":
        # Near step: everything below the knee wins, nothing above.
        return [1.0 if t < 0.25 else max(0.0, 0.02 * (1.0 - t)) for t in ts]
    if name == "flat":
        return [0.37] * n
    if name == "convex":
        return [(1.0 - t) ** 3 for t in ts]
    if name == "concave":
        return [1.0 - t**3 for t in ts]
    if name == "noisy":
        # A finite simulation genuinely produces non-monotone curves.
        return [rng.random() for _ in ts]
    if name == "zero":
        return [0.0] * n
    if name == "saturated":
        return [1.0] * n
    raise AssertionError(f"unknown shape {name}")


SHAPES = ("monotone", "steep", "flat", "convex", "concave", "noisy", "zero", "saturated")


def _assert_hard_invariants(
    result: OptimizerResult,
    constraints: OptimizerConstraints,
    curve: list[tuple[float, float]],
    label: str,
) -> None:
    """The red gate itself. Every clause here is an automatic NO-GO if violated."""
    if not result.is_feasible:
        assert result.recommended_bid is None, label
        assert result.infeasible_reason, f"{label}: infeasible without a reason"
        assert result.expected_contribution is None, label
        assert result.win_probability is None, label
        return

    bid = result.recommended_bid
    assert bid is not None and math.isfinite(bid) and bid > 0.0, label

    # 1. hard cost floor
    if constraints.hard_cost_floor is not None:
        assert bid >= float(constraints.hard_cost_floor) - EXACT, (
            f"{label}: RED GATE bid {bid!r} below hard_cost_floor "
            f"{constraints.hard_cost_floor!r}"
        )

    # 2. minimum margin, recomputed from scratch
    achieved = margin_pct(bid, float(constraints.estimated_cost))
    assert achieved >= float(constraints.min_margin_pct) - EXACT, (
        f"{label}: RED GATE margin {achieved!r}% below min_margin_pct "
        f"{constraints.min_margin_pct!r} at bid {bid!r} cost {constraints.estimated_cost!r}"
    )
    assert result.margin_pct == pytest.approx(achieved, rel=1e-12), label

    # 3. target win probability
    probability = win_probability_at(curve, bid)
    assert result.win_probability == pytest.approx(probability, rel=1e-12, abs=1e-15), label
    if constraints.target_win_probability is not None:
        assert probability >= float(constraints.target_win_probability) - EXACT, (
            f"{label}: RED GATE win probability {probability!r} below target "
            f"{constraints.target_win_probability!r} at bid {bid!r}"
        )

    # 4. ceilings and the simulated grid (never extrapolate)
    if constraints.max_bid is not None:
        assert bid <= float(constraints.max_bid) + EXACT, f"{label}: bid above max_bid"
    assert curve[0][0] <= bid <= curve[-1][0], f"{label}: bid outside the simulated grid"

    # 5. the reported contribution is the objective at the reported bid
    expected = probability * (bid - constraints.contribution_base)
    assert result.expected_contribution == pytest.approx(expected, rel=1e-9, abs=1e-9), label

    # 6. the advertised bands are themselves feasible - a "safe range" that
    #    dips below the cost floor would leak a non-compliant price into the UI.
    for name, band in (("safe_range", result.safe_range), ("aggr", result.aggressive_range)):
        if band is None:
            continue
        low, high = band
        assert low <= high, f"{label}: {name} is inverted"
        for edge in band:
            assert margin_pct(edge, float(constraints.estimated_cost)) >= (
                float(constraints.min_margin_pct) - EXACT
            ), f"{label}: RED GATE {name} edge {edge!r} below min_margin_pct"
            if constraints.hard_cost_floor is not None:
                assert edge >= float(constraints.hard_cost_floor) - EXACT, (
                    f"{label}: RED GATE {name} edge {edge!r} below hard_cost_floor"
                )
            if constraints.max_bid is not None:
                assert edge <= float(constraints.max_bid) + EXACT, f"{label}: {name} above max_bid"


# --- 1. property sweep -------------------------------------------------------


def _sweep_cases() -> list[tuple]:
    """Deterministic cross-product of adversarial constraint combinations."""
    rng = random.Random(20260910)
    cases: list[tuple] = []
    grids = [
        [100.0 + 100.0 * i for i in range(9)],          # ordinary
        [1.0, 2.0],                                      # two points
        [500.0],                                         # single point
        [1e6 * (1.0 + 0.25 * i) for i in range(5)],      # SAR-scale money
        [1e-3 * (1.0 + i) for i in range(4)],            # sub-riyal, stresses ULPs
    ]
    for grid, shape in itertools.product(grids, SHAPES):
        probabilities = _shape(shape, len(grid), rng)
        curve = _curve(grid, probabilities)
        lo, hi = grid[0], grid[-1]
        for cost_factor, margin, floor_factor, target, reserve_factor, cap_factor in itertools.product(
            (0.0, 0.4, 1.0, 1.8),
            (0.0, 25.0, 60.0, 99.9, 99.999999),
            (None, 0.0, 0.5, 1.0, 1.4),
            (None, 0.0, 0.5, 0.999, 1.0),
            (0.0, 0.3),
            (None, 0.7, 1.0),
        ):
            cases.append(
                (
                    curve,
                    lo * cost_factor,
                    margin,
                    None if floor_factor is None else lo * floor_factor,
                    target,
                    lo * reserve_factor,
                    None if cap_factor is None else max(hi * cap_factor, 1e-9),
                )
            )
    rng.shuffle(cases)
    return cases


SWEEP_CASES = _sweep_cases()


def test_sweep_is_large_enough_to_be_evidence():
    assert len(SWEEP_CASES) >= 500, len(SWEEP_CASES)


def test_hard_constraints_are_never_violated_across_the_sweep():
    """The red gate, over every sweep combination at once.

    Kept as a single test so a failure reports EVERY violating combination
    rather than only the first one pytest happens to run.
    """
    violations: list[str] = []
    feasible = 0
    infeasible = 0
    for index, (curve, cost, margin, floor, target, reserve, cap) in enumerate(SWEEP_CASES):
        constraints = OptimizerConstraints(
            estimated_cost=cost,
            min_margin_pct=margin,
            hard_cost_floor=floor,
            target_win_probability=target,
            risk_reserve=reserve,
            max_bid=cap,
        )
        result = optimize(curve=curve, constraints=constraints)
        if result.is_feasible:
            feasible += 1
        else:
            infeasible += 1
        try:
            _assert_hard_invariants(result, constraints, curve, f"case#{index}")
        except AssertionError as exc:  # collect, do not stop
            violations.append(str(exc))
    assert not violations, (
        f"{len(violations)} hard-constraint violations out of {len(SWEEP_CASES)}:\n"
        + "\n".join(violations[:20])
    )
    # Both branches must actually be exercised, otherwise the sweep proves nothing.
    assert feasible > 100, feasible
    assert infeasible > 100, infeasible


def test_sweep_results_are_deterministic():
    """Same inputs, same recommendation - the optimizer holds no state."""
    for curve, cost, margin, floor, target, reserve, cap in SWEEP_CASES[:200]:
        kwargs = dict(
            estimated_cost=cost,
            min_margin_pct=margin,
            hard_cost_floor=floor,
            target_win_probability=target,
            risk_reserve=reserve,
            max_bid=cap,
        )
        first = optimize(curve=curve, constraints=OptimizerConstraints(**kwargs))
        second = optimize(curve=list(reversed(curve)), constraints=OptimizerConstraints(**kwargs))
        assert first.to_dict() == second.to_dict()


def test_optimizer_output_survives_the_prediction_contract():
    """to_price_prediction must not manufacture a band below the margin floor."""
    checked = 0
    for curve, cost, margin, floor, target, reserve, cap in SWEEP_CASES[:400]:
        constraints = OptimizerConstraints(
            estimated_cost=cost,
            min_margin_pct=margin,
            hard_cost_floor=floor,
            target_win_probability=target,
            risk_reserve=reserve,
            max_bid=cap,
        )
        result = optimize(curve=curve, constraints=constraints)
        prediction = to_price_prediction(result, tender_id=42, constraints=constraints)
        if not result.is_feasible:
            assert prediction.is_suppressed
            assert prediction.p50 is None
            continue
        checked += 1
        assert not prediction.is_suppressed
        assert prediction.p10 <= prediction.p50 <= prediction.p90
        for quantile in (prediction.p10, prediction.p50, prediction.p90):
            assert margin_pct(quantile, cost) >= margin - EXACT, (
                f"RED GATE prediction band value {quantile!r} below min_margin_pct {margin!r}"
            )
            if floor is not None:
                assert quantile >= floor - EXACT
    assert checked > 50, checked


# --- 2. brute-force agreement ------------------------------------------------

BRUTE_STEPS = 4000


def _brute_force(curve, constraints: OptimizerConstraints):
    """Exhaustive scan of the grid under the SAME constraints, computed independently."""
    lo, hi = curve[0][0], curve[-1][0]
    floor = min_margin_price(float(constraints.estimated_cost), float(constraints.min_margin_pct))
    base = constraints.contribution_base
    best_price = None
    best_value = -math.inf
    for step in range(BRUTE_STEPS + 1):
        price = lo if hi == lo else lo + (hi - lo) * step / BRUTE_STEPS
        if price <= 0.0:
            continue
        if floor is None or price < floor:
            continue
        if constraints.hard_cost_floor is not None and price < float(constraints.hard_cost_floor):
            continue
        if margin_pct(price, float(constraints.estimated_cost)) < float(constraints.min_margin_pct):
            continue
        if constraints.max_bid is not None and price > float(constraints.max_bid):
            continue
        probability = win_probability_at(curve, price)
        if (
            constraints.target_win_probability is not None
            and probability < float(constraints.target_win_probability)
        ):
            continue
        value = probability * (price - base)
        if value > best_value:
            best_value, best_price = value, price
    return best_price, best_value


BRUTE_CASES = [case for case in SWEEP_CASES if len(case[0]) > 1][:120]


def test_brute_force_scan_agrees_with_the_closed_form_argmax():
    """An exhaustive scan must never beat the analytic optimum by more than tolerance."""
    compared = 0
    worse: list[str] = []
    for index, (curve, cost, margin, floor, target, reserve, cap) in enumerate(BRUTE_CASES):
        constraints = OptimizerConstraints(
            estimated_cost=cost,
            min_margin_pct=margin,
            hard_cost_floor=floor,
            target_win_probability=target,
            risk_reserve=reserve,
            max_bid=cap,
        )
        result = optimize(curve=curve, constraints=constraints)
        brute_price, brute_value = _brute_force(curve, constraints)
        if not result.is_feasible:
            # The optimizer refused; the brute force must agree there is nothing there.
            assert brute_price is None, (
                f"case#{index}: optimizer said infeasible "
                f"({result.infeasible_reason}) but brute force found {brute_price!r}"
            )
            continue
        assert brute_price is not None, f"case#{index}: brute force found nothing feasible"
        compared += 1
        got = result.expected_contribution
        scale = max(abs(brute_value), abs(got), 1.0)
        if brute_value > got + 1e-6 * scale:
            worse.append(
                f"case#{index}: brute force {brute_value:.10g} at {brute_price:.10g} "
                f"beats optimizer {got:.10g} at {result.recommended_bid:.10g}"
            )
    assert not worse, "\n".join(worse[:10])
    assert compared > 20, compared


def test_closed_form_beats_the_grid_on_an_interior_vertex():
    """The analytic vertex is exact, so it must be at least as good as any grid point."""
    curve = [(100.0, 1.0), (300.0, 0.0)]
    constraints = OptimizerConstraints(estimated_cost=50.0, min_margin_pct=0.0)
    result = optimize(curve=curve, constraints=constraints)
    # p(b) = (300-b)/200 ; f(b) = p(b)*(b-50) ; vertex at b = 175
    assert result.recommended_bid == pytest.approx(175.0)
    _brute_price, brute_value = _brute_force(curve, constraints)
    assert result.expected_contribution >= brute_value - 1e-9


# --- 3. infeasible detection -------------------------------------------------


def test_steep_curve_with_high_target_and_high_margin_is_explicitly_infeasible():
    """The headline conflict: 95% win probability under a 40% margin floor."""
    # Below 130 the bid wins almost always; above it, almost never.
    curve = [(100.0, 0.99), (120.0, 0.97), (130.0, 0.50), (140.0, 0.03), (400.0, 0.0)]
    # A 40% margin on a cost of 120 forces the bid to at least 200, where the
    # simulated win probability is ~0.
    constraints = OptimizerConstraints(
        estimated_cost=120.0, min_margin_pct=40.0, target_win_probability=0.95
    )
    result = optimize(curve=curve, constraints=constraints)
    assert result.recommended_bid is None
    assert result.is_feasible is False
    assert result.expected_contribution is None
    assert result.win_probability is None
    assert result.margin_pct is None
    reason = result.infeasible_reason
    assert reason
    assert CONSTRAINT_TARGET_WIN_PROBABILITY in reason
    assert CONSTRAINT_MIN_MARGIN in reason
    assert "0.95" in reason
    payload = result.to_dict()
    assert payload["recommended_bid"] is None and payload["is_feasible"] is False
    assert payload["infeasible_reason"] == reason


@pytest.mark.parametrize(
    ("kwargs", "expected_token"),
    [
        # margin floor above the whole grid
        (dict(estimated_cost=1000.0, min_margin_pct=90.0), CONSTRAINT_MIN_MARGIN),
        # hard floor above the whole grid
        (
            dict(estimated_cost=10.0, min_margin_pct=0.0, hard_cost_floor=10_000.0),
            CONSTRAINT_HARD_COST_FLOOR,
        ),
        # ceiling below the margin floor
        (
            dict(estimated_cost=200.0, min_margin_pct=50.0, max_bid=390.0),
            CONSTRAINT_MAX_BID,
        ),
        # unreachable margin: 100% requires a zero cost
        (dict(estimated_cost=100.0, min_margin_pct=100.0), "100"),
        (dict(estimated_cost=100.0, min_margin_pct=250.0), "250"),
    ],
)
def test_conflicting_constraints_return_a_named_reason_not_a_number(kwargs, expected_token):
    curve = [(100.0, 0.9), (200.0, 0.6), (400.0, 0.1)]
    result = optimize(curve=curve, constraints=OptimizerConstraints(**kwargs))
    assert result.recommended_bid is None, kwargs
    assert result.infeasible_reason and expected_token in result.infeasible_reason


def test_infeasible_never_returns_the_closest_violating_price():
    """The classic red-gate failure: 'nearest feasible-ish price' instead of None."""
    curve = [(100.0, 1.0), (200.0, 0.0)]
    result = optimize(
        curve=curve,
        constraints=OptimizerConstraints(estimated_cost=180.0, min_margin_pct=50.0),
    )
    # The margin floor is 360, off the top of the grid.
    assert result.recommended_bid is None
    assert "360" in result.infeasible_reason


def test_result_object_refuses_to_hold_both_a_bid_and_a_reason():
    with pytest.raises(OptimizerError):
        OptimizerResult(recommended_bid=100.0, infeasible_reason="conflict")
    with pytest.raises(OptimizerError):
        OptimizerResult()


# --- 4. binding constraints on boundary optima -------------------------------


def test_optimum_pinned_to_the_margin_floor_reports_it():
    # p(b) = (1000-b)/900. With cost 400 the unconstrained vertex sits at 700,
    # so a 50% margin floor (= 800) binds and the optimum is pushed up to it.
    curve = [(100.0, 1.0), (1000.0, 0.0)]
    free = optimize(
        curve=curve,
        constraints=OptimizerConstraints(estimated_cost=400.0, min_margin_pct=20.0),
    )
    assert free.recommended_bid == pytest.approx(700.0, rel=1e-9)
    assert CONSTRAINT_MIN_MARGIN not in free.binding_constraints

    result = optimize(
        curve=curve,
        constraints=OptimizerConstraints(estimated_cost=400.0, min_margin_pct=50.0),
    )
    assert result.recommended_bid == pytest.approx(800.0, rel=1e-9)
    assert result.margin_pct >= 50.0
    assert CONSTRAINT_MIN_MARGIN in result.binding_constraints


def test_optimum_pinned_to_the_hard_cost_floor_reports_it():
    curve = [(100.0, 1.0), (1000.0, 0.0)]
    result = optimize(
        curve=curve,
        constraints=OptimizerConstraints(
            estimated_cost=100.0, min_margin_pct=0.0, hard_cost_floor=620.0
        ),
    )
    assert result.recommended_bid == pytest.approx(620.0)
    assert CONSTRAINT_HARD_COST_FLOOR in result.binding_constraints


def test_optimum_pinned_to_max_bid_reports_it():
    # Rising win probability with price is unphysical but a finite simulation can
    # produce it; the optimum then sits on the ceiling.
    curve = [(100.0, 0.1), (1000.0, 0.9)]
    result = optimize(
        curve=curve,
        constraints=OptimizerConstraints(estimated_cost=10.0, min_margin_pct=0.0, max_bid=700.0),
    )
    assert result.recommended_bid == pytest.approx(700.0)
    assert CONSTRAINT_MAX_BID in result.binding_constraints


def test_optimum_pinned_to_the_grid_edges_reports_grid_censoring():
    rising = optimize(
        curve=[(100.0, 0.1), (1000.0, 0.9)],
        constraints=OptimizerConstraints(estimated_cost=10.0, min_margin_pct=0.0),
    )
    assert rising.recommended_bid == pytest.approx(1000.0)
    assert CONSTRAINT_GRID_UPPER in rising.binding_constraints

    falling = optimize(
        curve=[(100.0, 0.9), (1000.0, 0.1)],
        constraints=OptimizerConstraints(estimated_cost=0.0, min_margin_pct=0.0),
    )
    # cost 0 and no floor: the lower grid edge is the binding lower bound.
    assert falling.recommended_bid >= 100.0
    if falling.recommended_bid == pytest.approx(100.0):
        assert CONSTRAINT_GRID_LOWER in falling.binding_constraints


def test_optimum_sitting_on_the_win_probability_target_reports_it():
    curve = [(100.0, 1.0), (500.0, 0.0)]
    result = optimize(
        curve=curve,
        constraints=OptimizerConstraints(
            estimated_cost=50.0, min_margin_pct=0.0, target_win_probability=0.6
        ),
    )
    # p(b) = (500-b)/400 ; the unconstrained vertex is 275 where p = 0.5625.
    # A 0.6 target caps the bid at 260, so the target binds there.
    assert result.recommended_bid == pytest.approx(260.0, rel=1e-9)
    assert result.win_probability >= 0.6
    assert CONSTRAINT_TARGET_WIN_PROBABILITY in result.binding_constraints


def test_every_binding_constraint_is_really_binding():
    """A reported binding constraint must actually sit at the recommended bid."""
    for curve, cost, margin, floor, target, reserve, cap in SWEEP_CASES[:400]:
        constraints = OptimizerConstraints(
            estimated_cost=cost,
            min_margin_pct=margin,
            hard_cost_floor=floor,
            target_win_probability=target,
            risk_reserve=reserve,
            max_bid=cap,
        )
        result = optimize(curve=curve, constraints=constraints)
        if not result.is_feasible:
            assert result.binding_constraints == []
            continue
        bid = result.recommended_bid
        for name in result.binding_constraints:
            if name == CONSTRAINT_HARD_COST_FLOOR:
                assert floor is not None and math.isclose(bid, floor, rel_tol=1e-9, abs_tol=1e-9)
            elif name == CONSTRAINT_MAX_BID:
                assert cap is not None and math.isclose(bid, cap, rel_tol=1e-9, abs_tol=1e-9)
            elif name == CONSTRAINT_GRID_LOWER:
                assert math.isclose(bid, curve[0][0], rel_tol=1e-9, abs_tol=1e-9)
            elif name == CONSTRAINT_GRID_UPPER:
                assert math.isclose(bid, curve[-1][0], rel_tol=1e-9, abs_tol=1e-9)
            elif name == CONSTRAINT_TARGET_WIN_PROBABILITY:
                assert target is not None
                assert math.isclose(
                    result.win_probability, target, rel_tol=1e-9, abs_tol=1e-9
                )
            elif name == CONSTRAINT_MIN_MARGIN:
                floor_price = min_margin_price(cost, margin)
                assert floor_price is not None
                assert math.isclose(bid, floor_price, rel_tol=1e-9, abs_tol=1e-9)
            else:  # pragma: no cover
                raise AssertionError(f"unknown binding constraint {name}")


# --- 5. directional monotonicity ---------------------------------------------

# A non-increasing curve makes the objective supermodular in (bid, cost), so the
# argmax is provably non-decreasing in cost (Topkis). A non-monotone curve gives
# no such guarantee, which is why these cases use falling curves only.
_DIRECTIONAL_CURVES = [
    [(100.0 + 100.0 * i, 1.0 - i / 8.0) for i in range(9)],
    [(100.0, 1.0), (900.0, 0.0)],
    [(100.0, 0.8), (200.0, 0.8), (600.0, 0.3), (900.0, 0.05)],
    [(1e6, 0.95), (2e6, 0.4), (4e6, 0.02)],
]


@pytest.mark.parametrize("curve", _DIRECTIONAL_CURVES)
def test_increasing_cost_never_decreases_the_recommended_bid(curve):
    previous = None
    seen = 0
    for cost in (0.0, 10.0, 50.0, 120.0, 250.0, 400.0, 1e5, 5e5, 1.5e6):
        result = optimize(
            curve=curve,
            constraints=OptimizerConstraints(estimated_cost=cost, min_margin_pct=5.0),
        )
        if not result.is_feasible:
            previous = None  # the chain restarts once the feasible set empties
            continue
        seen += 1
        if previous is not None:
            assert result.recommended_bid >= previous - 1e-9, (
                f"cost {cost}: bid fell from {previous} to {result.recommended_bid}"
            )
        previous = result.recommended_bid
    assert seen >= 3, seen


@pytest.mark.parametrize("curve", _DIRECTIONAL_CURVES)
def test_increasing_min_margin_never_decreases_the_recommended_bid(curve):
    """Raising the margin floor only removes low prices, so the bid can only rise."""
    previous = None
    seen = 0
    cost = curve[0][0] * 0.5
    for margin in (0.0, 5.0, 15.0, 30.0, 45.0, 60.0, 75.0, 90.0, 99.0):
        result = optimize(
            curve=curve,
            constraints=OptimizerConstraints(estimated_cost=cost, min_margin_pct=margin),
        )
        if not result.is_feasible:
            previous = None
            continue
        seen += 1
        if previous is not None:
            assert result.recommended_bid >= previous - 1e-9, (
                f"min_margin {margin}: bid fell from {previous} to {result.recommended_bid}"
            )
        previous = result.recommended_bid
    assert seen >= 3, seen


@pytest.mark.parametrize("curve", _DIRECTIONAL_CURVES)
def test_raising_the_hard_floor_never_lowers_the_bid(curve):
    previous = None
    seen = 0
    lo, hi = curve[0][0], curve[-1][0]
    for fraction in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0, 1.5):
        floor = lo + (hi - lo) * fraction
        result = optimize(
            curve=curve,
            constraints=OptimizerConstraints(
                estimated_cost=lo * 0.2, min_margin_pct=0.0, hard_cost_floor=floor
            ),
        )
        if not result.is_feasible:
            previous = None
            continue
        seen += 1
        if previous is not None:
            assert result.recommended_bid >= previous - 1e-9
        previous = result.recommended_bid
    assert seen >= 3, seen


def test_monotonicity_holds_across_a_randomised_falling_curve_sweep():
    rng = random.Random(4242)
    for _ in range(150):
        n = rng.choice([2, 3, 5, 9, 17])
        lo = rng.choice([1.0, 100.0, 1e5])
        hi = lo * rng.choice([2.0, 8.0, 40.0])
        prices = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
        probabilities = sorted((rng.random() for _ in range(n)), reverse=True)
        curve = _curve(prices, probabilities)
        margin = rng.choice([0.0, 10.0, 35.0])
        floor = rng.choice([None, lo, lo * 1.2])
        previous = None
        for cost in sorted(rng.sample([0.0, lo * 0.2, lo * 0.6, lo, hi * 0.3, hi * 0.7], 4)):
            result = optimize(
                curve=curve,
                constraints=OptimizerConstraints(
                    estimated_cost=cost, min_margin_pct=margin, hard_cost_floor=floor
                ),
            )
            if not result.is_feasible:
                previous = None
                continue
            if previous is not None:
                assert result.recommended_bid >= previous - 1e-9, (
                    f"curve={curve} margin={margin} floor={floor} cost={cost}"
                )
            previous = result.recommended_bid


# --- 6. degenerate inputs ----------------------------------------------------


@pytest.mark.parametrize(
    "curve",
    [
        [],
        (),
        iter([]),
        {},
    ],
)
def test_empty_curve_raises_a_typed_error(curve):
    with pytest.raises(OptimizerError):
        optimize(curve=curve, constraints=OptimizerConstraints(100.0, 10.0))


@pytest.mark.parametrize("curve", [None, "100,0.5", b"x"])
def test_non_curve_input_raises_a_typed_error(curve):
    with pytest.raises(OptimizerError):
        optimize(curve=curve, constraints=OptimizerConstraints(100.0, 10.0))


def test_single_point_curve_is_handled_without_crashing():
    result = optimize(
        curve=[(500.0, 0.42)],
        constraints=OptimizerConstraints(estimated_cost=100.0, min_margin_pct=10.0),
    )
    assert result.recommended_bid == pytest.approx(500.0)
    assert result.win_probability == pytest.approx(0.42)
    assert margin_pct(500.0, 100.0) >= 10.0

    # Same single point, but the margin floor is above it: refuse, do not clamp.
    refused = optimize(
        curve=[(500.0, 0.42)],
        constraints=OptimizerConstraints(estimated_cost=400.0, min_margin_pct=50.0),
    )
    assert refused.recommended_bid is None
    assert refused.infeasible_reason


@pytest.mark.parametrize(
    "bad_cost",
    [None, float("nan"), float("inf"), float("-inf"), -1.0, -1e-12, "100", b"100", True, object()],
)
def test_bad_cost_raises_a_typed_error_never_a_recommendation(bad_cost):
    with pytest.raises(OptimizerError):
        OptimizerConstraints(estimated_cost=bad_cost, min_margin_pct=10.0)


@pytest.mark.parametrize("bad_margin", [None, float("nan"), float("inf"), "10", True])
def test_bad_margin_raises_a_typed_error(bad_margin):
    with pytest.raises(OptimizerError):
        OptimizerConstraints(estimated_cost=100.0, min_margin_pct=bad_margin)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hard_cost_floor", -1.0),
        ("hard_cost_floor", float("nan")),
        ("risk_reserve", -0.01),
        ("risk_reserve", float("inf")),
        ("target_win_probability", 1.0001),
        ("target_win_probability", -0.0001),
        ("target_win_probability", float("nan")),
        ("max_bid", 0.0),
        ("max_bid", -5.0),
    ],
)
def test_bad_optional_fields_raise_typed_errors(field, value):
    with pytest.raises(OptimizerError):
        OptimizerConstraints(estimated_cost=100.0, min_margin_pct=10.0, **{field: value})


def test_zero_margin_and_zero_cost_are_legal_and_produce_a_bid():
    result = optimize(
        curve=[(100.0, 0.9), (500.0, 0.1)],
        constraints=OptimizerConstraints(estimated_cost=0.0, min_margin_pct=0.0),
    )
    assert result.is_feasible
    assert result.recommended_bid >= 100.0
    assert result.margin_pct == pytest.approx(100.0)


def test_mutating_constraints_after_construction_is_re_validated():
    """The dataclass is mutable; optimize must not trust a stale __post_init__."""
    constraints = OptimizerConstraints(estimated_cost=100.0, min_margin_pct=10.0)
    constraints.estimated_cost = -5.0
    with pytest.raises(OptimizerError):
        optimize(curve=[(100.0, 0.5), (200.0, 0.2)], constraints=constraints)


def test_optimize_rejects_a_foreign_constraints_object():
    class Fake:
        estimated_cost = 100.0
        min_margin_pct = 0.0
        hard_cost_floor = None
        target_win_probability = None
        risk_reserve = 0.0
        max_bid = None

    with pytest.raises(OptimizerError):
        optimize(curve=[(100.0, 0.5)], constraints=Fake())


@pytest.mark.parametrize(
    "curve",
    [
        [(0.0, 0.5), (100.0, 0.1)],            # zero price
        [(-10.0, 0.5), (100.0, 0.1)],          # negative price
        [(100.0, 1.5), (200.0, 0.1)],          # probability above 1
        [(100.0, -0.1), (200.0, 0.1)],         # negative probability
        [(100.0, float("nan")), (200.0, 0.1)],  # NaN probability
        [(100.0, 0.5), (100.0, 0.9)],          # contradictory duplicate price
        [(100.0,), (200.0, 0.1)],              # malformed entry
        [{"price": 100.0}, (200.0, 0.1)],      # mapping missing a key
    ],
)
def test_malformed_curve_entries_raise_typed_errors(curve):
    with pytest.raises(OptimizerError):
        optimize(curve=curve, constraints=OptimizerConstraints(10.0, 0.0))


def test_agreeing_duplicate_prices_are_accepted():
    result = optimize(
        curve=[(100.0, 0.9), (100.0, 0.9), (400.0, 0.1)],
        constraints=OptimizerConstraints(estimated_cost=50.0, min_margin_pct=0.0),
    )
    assert result.is_feasible


def test_win_probability_at_never_extrapolates():
    curve = [(100.0, 0.9), (400.0, 0.1)]
    assert win_probability_at(curve, 250.0) == pytest.approx(0.5)
    with pytest.raises(OptimizerError):
        win_probability_at(curve, 99.0)
    with pytest.raises(OptimizerError):
        win_probability_at(curve, 401.0)


# --- 7. the margin floor itself ----------------------------------------------


def test_margin_floor_converges_for_extreme_margins():
    """DEFECT REGRESSION.

    ``min_margin_price`` used to walk upward by single ULPs, which cannot close
    the gap when ``min_margin_pct`` sits near 100%: the residual ``1 - cost/b``
    carries ~1e-16 of absolute error, many orders of magnitude more than 64 ULPs
    of ``b`` can move it. It raised
    ``OptimizerError('could not compute a margin-safe floor ...')`` - an internal
    failure, not an input error - for ~4.5% of a random (cost, margin) grid, and
    the console's POST /api/scenarios path calls it directly, so those inputs
    became a 500 rather than an honest answer. The nudge now doubles each step.
    """
    rng = random.Random(20260910)
    checked = 0
    for _ in range(20000):
        cost = rng.choice(
            [
                rng.uniform(1e-6, 1e-3),
                rng.uniform(0.01, 1.0),
                rng.uniform(1.0, 1e4),
                rng.uniform(1e4, 1e8),
            ]
        )
        margin = rng.choice(
            [rng.uniform(0.0, 99.0), rng.uniform(99.0, 99.9999), rng.uniform(99.9999, 100.0)]
        )
        floor = min_margin_price(cost, margin)
        assert floor is not None
        checked += 1
        if floor <= 0.0:
            continue
        assert margin_pct(floor, cost) >= margin, (cost, margin, floor)
        # And the floor must not be inflated: it stays within a whisker of the
        # closed form, so it never quietly pushes the recommendation upward.
        ideal = cost / (1.0 - margin / MAX_MARGIN_PCT)
        assert floor <= ideal * (1.0 + 1e-6) + 1e-12, (cost, margin, floor, ideal)
    assert checked == 20000


@pytest.mark.parametrize("margin", [99.0, 99.9, 99.99, 99.999, 99.999999, 99.99999999999])
@pytest.mark.parametrize("cost", [9.9e-07, 1e-4, 0.1400404401595807, 1.0, 1802.02, 5.48e7])
def test_named_regression_costs_and_margins_do_not_raise(cost, margin):
    """The exact (cost, margin) pairs the sweep first failed on."""
    floor = min_margin_price(cost, margin)
    assert floor is not None and floor > 0.0
    assert margin_pct(floor, cost) >= margin
    # And the full optimizer runs on them.
    grid = [floor * f for f in (0.5, 0.9, 1.0, 1.2, 2.0)]
    curve = _curve(grid, [0.9, 0.7, 0.5, 0.3, 0.05])
    result = optimize(
        curve=curve, constraints=OptimizerConstraints(estimated_cost=cost, min_margin_pct=margin)
    )
    assert result.is_feasible
    assert margin_pct(result.recommended_bid, cost) >= margin


def test_margin_floor_is_none_only_when_truly_unreachable():
    assert min_margin_price(100.0, 100.0) is None
    assert min_margin_price(100.0, 120.0) is None
    # A zero cost reaches 100% margin at any positive price.
    assert min_margin_price(0.0, 100.0) == 0.0
    assert min_margin_price(0.0, 250.0) == 0.0
    # A negative requirement does not bind.
    assert min_margin_price(100.0, -50.0) >= 0.0


def test_margin_pct_rejects_impossible_inputs():
    for bid in (0.0, -1.0, float("nan"), float("inf"), None):
        with pytest.raises(OptimizerError):
            margin_pct(bid, 10.0)
    with pytest.raises(OptimizerError):
        margin_pct(100.0, -1.0)


# --- 8. live API -------------------------------------------------------------


def _console_get(path: str, timeout: float = 20.0):
    request = urllib.request.Request(f"{CONSOLE_BASE}{path}", headers=auth_headers())
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def _console_post(path: str, payload: dict, timeout: float = 60.0):
    request = urllib.request.Request(
        f"{CONSOLE_BASE}{path}",
        data=json.dumps(payload).encode(),
        headers=auth_headers({"Content-Type": "application/json"}),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode())


def _amount(value) -> float:
    """Money crosses the API as {amount, currency, vat_semantics}; a bare
    number is accepted too so the red gate cannot be dodged by a format."""
    if isinstance(value, dict):
        assert value.get("currency") == "SAR", f"unexpected currency in {value}"
        return float(value["amount"])
    return float(value)


def _live_console_or_skip() -> None:
    try:
        _console_get("/api/settings", timeout=5.0)
    except urllib.error.HTTPError as exc:
        # The console answered, so it is up: a 401 means the credential is
        # missing or wrong, and skipping would hide the whole live layer.
        pytest.fail(f"console refused the battery's credential: HTTP {exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:  # pragma: no cover
        pytest.skip(f"console API not reachable at {CONSOLE_BASE}: {exc}")

# NOTE ON LIVE COVERAGE (honest limitation):
# On this corpus every scenario curve comes back suppressed with
# INSUFFICIENT_EVIDENCE - `candidate_bidders` returns no candidate that has both
# a participation estimate and a price range, so the console never reaches
# `optimizer.optimize` at all. These tests therefore prove that the live
# endpoint surfaces an explicit suppression or infeasible state rather than a
# number; they CANNOT prove the live feasible branch, because the data to
# produce one does not exist. That is stated here rather than faked with a
# seeded fixture pretending to be production.

MULTI_BIDDER_TENDER_IDS = (1692, 1613, 1557, 614, 211)


@pytest.mark.live_api
def test_live_scenario_endpoint_surfaces_infeasibility_instead_of_a_number():
    """POST a scenario with an absurd margin floor and read the curve back.

    The API must return an explicit optimizer infeasible state or an explicit
    suppression - never a recommended bid that violates the margin floor.
    """
    _live_console_or_skip()

    tenders = _console_get("/api/tenders?limit=40")
    items = tenders.get("items") or []
    assert items, "no tenders returned by the live console"
    available = {int(item["id"]) for item in items}
    targets = [tid for tid in MULTI_BIDDER_TENDER_IDS if tid in available] or [
        int(items[0]["id"])
    ]

    checked = 0
    for tender_id in targets[:2]:
        # 99.9% margin AND a 95% win-probability target: the margin floor is
        # ~1000x the cost, a price at which nothing wins.
        payload = {
            "estimated_cost": 100000.0,
            "min_margin_pct": 99.9,
            "target_win_pct": 95.0,
            "risk_reserve": 0.0,
            "name": "red-gate battery: absurd margin floor",
            "seed": 12345,
        }
        try:
            status, created = _console_post(f"/api/tenders/{tender_id}/scenarios", payload)
        except urllib.error.HTTPError as exc:  # pragma: no cover
            pytest.fail(f"scenario create failed for tender {tender_id}: {exc.read()!r}")
        assert status == 200
        scenario_id = created["scenario"]["id"]

        try:
            curve = _console_get(
                f"/api/scenarios/{scenario_id}/curve?refresh=true", timeout=180.0
            )
        except urllib.error.HTTPError as exc:  # pragma: no cover
            pytest.fail(
                f"scenario curve failed for tender {tender_id}: {exc.code} {exc.read()!r}"
            )
        checked += 1

        # Whatever happens, the response must not contain a bid recommendation.
        assert curve["cache"] == "miss"
        if curve["suppressed"]:
            assert curve["optimizer"] is None
            assert curve["suppression_reason"], "suppressed without a machine-readable reason"
            assert curve["curve"] == []
            continue

        optimizer = curve["optimizer"]
        assert optimizer is not None
        cost = _amount(curve["constraints"]["estimated_cost"])
        min_margin = float(curve["constraints"]["min_margin_pct"])
        if optimizer["recommended_bid"] is None:
            assert optimizer["infeasible_reason"], "infeasible without a reason"
            assert optimizer["is_feasible"] is False
        else:
            bid = _amount(optimizer["recommended_bid"])
            assert margin_pct(bid, cost) >= min_margin, (
                f"RED GATE: live API recommended {bid} on tender {tender_id}, "
                f"margin {margin_pct(bid, cost)}% < {min_margin}%"
            )
    assert checked > 0, "no live scenario could be evaluated"


@pytest.mark.live_api
def test_live_scenario_endpoint_never_500s_on_extreme_margin_inputs():
    """The margin-floor convergence defect, driven through the real API.

    Before the fix, `min_margin_price` raised OptimizerError for a wide band of
    (cost, margin) pairs; the console calls it directly while building the price
    grid, so those inputs became a 500. The console's own column is
    numeric(6,2), which happens to mask most of the band - this test drives what
    the API actually accepts and asserts a clean answer.
    """
    _live_console_or_skip()
    for cost, margin in ((0.01, 99.99), (1802.02, 99.99), (1e8, 99.99), (0.01, 0.0)):
        status, created = _console_post(
            f"/api/tenders/{MULTI_BIDDER_TENDER_IDS[0]}/scenarios",
            {
                "estimated_cost": cost,
                "min_margin_pct": margin,
                "seed": 777,
                "name": "red-gate battery: extreme margin floor",
            },
        )
        assert status == 200
        scenario_id = created["scenario"]["id"]
        try:
            curve = _console_get(
                f"/api/scenarios/{scenario_id}/curve?refresh=true", timeout=180.0
            )
        except urllib.error.HTTPError as exc:  # pragma: no cover
            pytest.fail(f"cost={cost} margin={margin} produced {exc.code}: {exc.read()!r}")
        assert "optimizer" in curve


@pytest.mark.live_api
def test_live_api_rejects_a_margin_the_column_cannot_store():
    """FIXED 2026-09-10 (was: rounding produced a value the validator forbids).

    ScenarioIn declared ``min_margin_pct < 100`` while user_bid_scenarios stores
    numeric(6,2), so 99.996 was accepted, rounded to 100.00 on write, and echoed
    back as a value the same validator forbids — and one the optimizer correctly
    calls unreachable at any price. The bound is now MAX_MARGIN_PCT = 99.99, the
    largest value the column can hold without rounding, so the request is
    refused up front by name instead of silently becoming a refusal later.
    """
    _live_console_or_skip()
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _console_post(
            f"/api/tenders/{MULTI_BIDDER_TENDER_IDS[0]}/scenarios",
            {
                "estimated_cost": 100000.0,
                "min_margin_pct": 99.996,
                "seed": 5,
                "name": "red-gate battery: margin rounding",
            },
        )
    assert excinfo.value.code == 422

    # The largest storable margin must round-trip EXACTLY, not get rounded.
    status_ok, created = _console_post(
        f"/api/tenders/{MULTI_BIDDER_TENDER_IDS[0]}/scenarios",
        {
            "estimated_cost": 100000.0,
            "min_margin_pct": 99.99,
            "seed": 5,
            "name": "red-gate battery: margin boundary",
        },
    )
    assert status_ok == 200, created
    assert created["scenario"]["inputs"]["min_margin_pct"] == 99.99

    # And the optimizer's behaviour at an unreachable margin is unchanged.
    assert min_margin_price(100000.0, 100.0) is None
    result = optimize(
        curve=[(1000.0, 0.9), (5000.0, 0.1)],
        constraints=OptimizerConstraints(estimated_cost=100000.0, min_margin_pct=100.0),
    )
    assert result.recommended_bid is None
    assert "100" in result.infeasible_reason


@pytest.mark.live_api
def test_live_scenario_endpoint_rejects_a_margin_of_100_percent():
    """A 100% margin is unreachable; the API's own validator must refuse it."""
    _live_console_or_skip()
    tenders = _console_get("/api/tenders?limit=1")
    items = tenders.get("items") or []
    assert items
    tender_id = items[0]["id"]
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _console_post(
            f"/api/tenders/{tender_id}/scenarios",
            {"estimated_cost": 1000.0, "min_margin_pct": 100.0},
        )
    assert excinfo.value.code == 422
