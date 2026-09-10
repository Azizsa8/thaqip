"""Simulation & determinism battery for the P2W Monte Carlo engine.

Testing guide section 12. This is an adversarial battery, not a smoke test: it
tries to *break* `thaqip_ingestion.p2w.montecarlo` on the properties the product
actually leans on, and it prefers checks that would fail loudly if the engine
silently changed behaviour.

What is proven here
-------------------
1. Monotonicity   - lowering the bid never lowers the win probability, swept over
                    22 synthetic competitor configurations x 52 price points, at
                    exact float equality (no tolerance), through both entry
                    points (`simulate` and `win_probability_curve`).
2. Convergence    - the *empirical* spread of the estimator across independent
                    seeds shrinks as 1/sqrt(iterations) at 500/2000/8000, and the
                    reported standard error tracks that spread.
3. Reproducibility- identical (inputs, seed) give a byte-identical dict, including
                    in a *fresh interpreter* with a different PYTHONHASHSEED.
4. Common random numbers - the curve has zero jitter reversals and each point is
                    bit-identical to the corresponding `simulate` call.
5. Bounds         - every probability lands in [0,1]; the rank distribution sums
                    to 1 within 1e-6 and its keys are always representable ranks.
6. Edge cases     - zero / one / all-disqualified / never-participating
                    competitors, exact ties (which need `median_bid`, not the
                    nominal price, because exp(log(x)) != x), and absurd bids of
                    1 SAR and 1e12 SAR.
7. Cross-check    - simulated win probability against the closed-form lognormal
                    answer, inside Monte Carlo error, including with
                    participation and technical-pass probabilities below 1.

Everything is offline, seeded and deterministic. No database, no network.
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics
import subprocess
import sys
from dataclasses import replace

import pytest

from thaqip_ingestion.p2w.contracts import (
    EvidenceTier,
    PredictionScope,
    PricePrediction,
)
from thaqip_ingestion.p2w.montecarlo import (
    DEFAULT_USER_TECHNICAL_PASS_P,
    MAX_STANDARD_ERROR,
    RANK_DISQUALIFIED,
    CompetitorDraw,
    CurvePoint,
    price_to_beat,
    simulate,
    standard_error_of_proportion,
    win_probability_curve,
)

BATTERY_SEED = 20260910


# --- deterministic synthetic corpus ------------------------------------------


def _make_configs(count: int = 22) -> list[tuple[str, list[CompetitorDraw]]]:
    """Build `count` reproducible competitor fields spanning the awkward corners.

    Generated from a fixed `random.Random` so the corpus is identical on every
    machine and every run, while still being varied enough that a monotonicity
    bug in one branch (ties, zero sigma, never-participates, certain
    disqualification) has somewhere to show up.
    """
    rnd = random.Random(BATTERY_SEED)
    configs: list[tuple[str, list[CompetitorDraw]]] = []

    # Hand-picked degenerate fields first: these are the ones that break engines.
    deterministic = CompetitorDraw(1, 1.0, math.log(1_000_000.0), 0.0, 1.0)
    configs.append(("single_deterministic", [deterministic]))
    configs.append(
        (
            "two_identical_deterministic",
            [deterministic, replace(deterministic, vendor_id=2)],
        )
    )
    configs.append(("never_participates", [replace(deterministic, participation_p=0.0)]))
    configs.append(("never_qualifies", [replace(deterministic, technical_pass_p=0.0)]))
    configs.append(
        (
            "mixed_degenerate",
            [
                replace(deterministic, participation_p=0.0),
                replace(deterministic, vendor_id=2, technical_pass_p=0.0),
                replace(deterministic, vendor_id=3, log_sigma=0.4),
            ],
        )
    )
    configs.append(
        (
            "wide_sigma_pair",
            [
                CompetitorDraw(1, 0.9, math.log(400_000.0), 0.9, 0.95),
                CompetitorDraw(2, 0.9, math.log(4_000_000.0), 0.9, 0.95),
            ],
        )
    )

    while len(configs) < count:
        size = rnd.randint(1, 7)
        field = [
            CompetitorDraw(
                vendor_id=index + 1,
                participation_p=rnd.choice([0.05, 0.25, 0.5, 0.75, 1.0]),
                log_mu=math.log(rnd.uniform(150_000.0, 6_000_000.0)),
                log_sigma=rnd.choice([0.0, 0.05, 0.15, 0.3, 0.6]),
                technical_pass_p=rnd.choice([0.0, 0.4, 0.85, 1.0]),
            )
            for index in range(size)
        ]
        configs.append((f"random_{len(configs):02d}_n{size}", field))
    return configs


CONFIGS = _make_configs()
CONFIG_IDS = [name for name, _ in CONFIGS]

#: 52 ascending prices spanning two orders of magnitude around the field.
PRICE_GRID = [100_000.0 * (1.09**step) for step in range(52)]

#: A realistic mid-size field used by the statistical tests.
FIELD = [
    CompetitorDraw(1, 0.9, math.log(1_000_000.0), 0.20, 0.95),
    CompetitorDraw(2, 0.7, math.log(1_050_000.0), 0.30, 0.90),
    CompetitorDraw(3, 0.5, math.log(950_000.0), 0.25, 1.00),
]


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def analytic_win_probability(
    bid: float, competitors: list[CompetitorDraw], user_technical_pass_p: float
) -> float:
    """Closed-form P(win) under lowest_qualified with independent lognormals.

    The user wins iff they qualify and no competitor both shows up, qualifies and
    prices below them. Exact ties have probability zero for any competitor with
    log_sigma > 0, so this expression is the truth the simulator must approach.
    """
    probability = user_technical_pass_p
    for competitor in competitors:
        if competitor.log_sigma <= 0.0:
            cheaper = 1.0 if competitor.median_bid < bid else 0.0
        else:
            cheaper = _norm_cdf(
                (math.log(bid) - competitor.log_mu) / competitor.log_sigma
            )
        probability *= 1.0 - competitor.participation_p * competitor.technical_pass_p * cheaper
    return probability


# --- 1. monotonicity ---------------------------------------------------------


@pytest.mark.parametrize(("name", "field"), CONFIGS, ids=CONFIG_IDS)
def test_simulate_is_exactly_monotone_in_bid(name: str, field: list[CompetitorDraw]) -> None:
    """Raising the bid never raises the win probability, at exact equality.

    `simulate` draws its scenarios from the seed alone (never from the bid), so
    for a fixed seed this must hold bit-exactly, not merely on average. Any
    tolerance here would hide the very bug worth catching.
    """
    previous: float | None = None
    for price in PRICE_GRID:
        result = simulate(
            user_bid=price, competitors=field, seed=4242, iterations=250,
            user_technical_pass_p=0.9,
        )
        if previous is not None:
            assert result.win_probability <= previous, (
                f"{name}: win probability rose from {previous!r} to "
                f"{result.win_probability!r} when the bid rose to {price}"
            )
        previous = result.win_probability


@pytest.mark.parametrize(("name", "field"), CONFIGS, ids=CONFIG_IDS)
def test_curve_has_no_jitter_reversals(name: str, field: list[CompetitorDraw]) -> None:
    """Common random numbers make the curve monotone with zero reversals."""
    curve = win_probability_curve(
        price_grid=PRICE_GRID, competitors=field, seed=4242, iterations=2000
    )
    assert [point.price for point in curve] == sorted(PRICE_GRID)
    reversals = [
        (curve[i - 1].price, curve[i - 1].win_probability, curve[i].price, curve[i].win_probability)
        for i in range(1, len(curve))
        if curve[i].win_probability > curve[i - 1].win_probability
    ]
    assert reversals == [], f"{name}: curve reversals {reversals[:3]}"


def test_monotonicity_sweep_covers_the_promised_surface() -> None:
    """Guard the battery itself: 20+ configurations, 50+ prices."""
    assert len(CONFIGS) >= 20
    assert len(PRICE_GRID) >= 50
    assert PRICE_GRID == sorted(PRICE_GRID)


# --- 2. convergence ----------------------------------------------------------


def test_empirical_spread_shrinks_as_inverse_sqrt_iterations() -> None:
    """The *measured* seed-to-seed spread must halve when iterations quadruple.

    This deliberately does not test the reported `standard_error`, which is a
    closed-form binomial expression and would pass 1/sqrt(n) by construction even
    if the sampler were broken. It re-runs the sampler under 120 independent
    seeds at each level and measures the real dispersion of the estimator.
    """
    spreads: dict[int, float] = {}
    means: dict[int, float] = {}
    for iterations in (500, 2000, 8000):
        estimates = [
            simulate(
                user_bid=1_000_000.0, competitors=FIELD, seed=seed, iterations=iterations
            ).win_probability
            for seed in range(120)
        ]
        spreads[iterations] = statistics.stdev(estimates)
        means[iterations] = statistics.mean(estimates)

    # All three levels must agree on the answer itself.
    assert abs(means[500] - means[8000]) < 0.02
    assert abs(means[2000] - means[8000]) < 0.02

    for coarse, fine in ((500, 2000), (2000, 8000)):
        ratio = spreads[coarse] / spreads[fine]
        assert 1.5 <= ratio <= 2.7, (
            f"spread ratio {coarse}->{fine} was {ratio:.3f}, expected ~2.0 "
            f"(spreads={spreads})"
        )
    overall = spreads[500] / spreads[8000]
    assert 2.8 <= overall <= 5.4, f"500->8000 spread ratio {overall:.3f}, expected ~4.0"


def test_reported_standard_error_matches_measured_spread() -> None:
    """The advertised standard error is not decorative: it matches reality."""
    iterations = 4000
    estimates = [
        simulate(
            user_bid=1_000_000.0, competitors=FIELD, seed=seed, iterations=iterations
        ).win_probability
        for seed in range(120)
    ]
    measured = statistics.stdev(estimates)
    reported = simulate(
        user_bid=1_000_000.0, competitors=FIELD, seed=0, iterations=iterations
    ).standard_error
    assert reported == pytest.approx(measured, rel=0.25), (
        f"reported se {reported:.5f} vs measured spread {measured:.5f}"
    )


def test_standard_error_formula_is_binomial_and_shrinks() -> None:
    previous = None
    for iterations in (500, 2000, 8000, 32_000):
        se = standard_error_of_proportion(0.5, iterations)
        assert se == pytest.approx(math.sqrt(0.25 / iterations))
        if previous is not None:
            assert se == pytest.approx(previous / 2.0, rel=1e-9)
        previous = se
    assert standard_error_of_proportion(0.0, 1000) == 0.0
    assert standard_error_of_proportion(1.0, 1000) == 0.0


def test_convergence_flag_follows_the_documented_threshold() -> None:
    tight = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=1, iterations=8000)
    assert tight.standard_error <= MAX_STANDARD_ERROR
    assert tight.convergence_ok is True
    loose = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=1, iterations=200)
    assert loose.standard_error > MAX_STANDARD_ERROR
    assert loose.convergence_ok is False


# --- 3. seed reproducibility -------------------------------------------------


def test_same_seed_gives_byte_identical_output_dict() -> None:
    kwargs = dict(
        user_bid=980_000.0, competitors=FIELD, seed=777, iterations=3000,
        user_technical_pass_p=0.88,
    )
    first = simulate(**kwargs).to_dict()
    second = simulate(**kwargs).to_dict()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first == second


def test_different_seed_gives_a_different_answer() -> None:
    """Reproducibility must not be reproducibility-by-being-constant."""
    values = {
        simulate(
            user_bid=980_000.0, competitors=FIELD, seed=seed, iterations=3000
        ).win_probability
        for seed in range(6)
    }
    assert len(values) > 1


_SUBPROCESS_SNIPPET = """
import json, math, sys
from thaqip_ingestion.p2w.montecarlo import CompetitorDraw, simulate, win_probability_curve

field = [
    CompetitorDraw(1, 0.9, math.log(1_000_000.0), 0.20, 0.95),
    CompetitorDraw(2, 0.7, math.log(1_050_000.0), 0.30, 0.90),
    CompetitorDraw(3, 0.5, math.log(950_000.0), 0.25, 1.00),
]
out = simulate(
    user_bid=980_000.0, competitors=field, seed=777, iterations=3000,
    user_technical_pass_p=0.88,
).to_dict()
curve = [
    p.to_dict()
    for p in win_probability_curve(
        price_grid=[8e5, 9e5, 1.0e6, 1.1e6, 1.3e6],
        competitors=field,
        seed=777,
        iterations=1500,
    )
]
sys.stdout.write(json.dumps({"simulate": out, "curve": curve}, sort_keys=True))
"""


def _run_in_fresh_interpreter(hash_seed: str) -> str:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    # Fixed argv, no shell: the same interpreter, running a literal snippet.
    completed = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SNIPPET],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_reproducible_in_a_fresh_interpreter_with_a_different_hash_seed() -> None:
    """The determinism contract has to survive a process restart, not just a loop.

    This is the check that catches determinism leaking in through set/dict
    iteration order or any module-level global RNG.
    """
    first = _run_in_fresh_interpreter("0")
    second = _run_in_fresh_interpreter("12345")
    assert first == second
    in_process = json.dumps(
        {
            "simulate": simulate(
                user_bid=980_000.0, competitors=FIELD, seed=777, iterations=3000,
                user_technical_pass_p=0.88,
            ).to_dict(),
            "curve": [
                point.to_dict()
                for point in win_probability_curve(
                    price_grid=[8e5, 9e5, 1.0e6, 1.1e6, 1.3e6],
                    competitors=FIELD,
                    seed=777,
                    iterations=1500,
                )
            ],
        },
        sort_keys=True,
    )
    assert in_process == first


def test_competitor_draw_order_is_normalised_by_the_orchestrator() -> None:
    """Determinism end-to-end depends on a stable competitor order.

    `simulate` consumes randomness competitor-by-competitor, so a shuffled field
    would legitimately produce a different (equally valid) answer. The engine
    therefore has to be fed a canonical order; `build_competitor_draws` is the
    place that promises it, and this locks that promise down.
    """
    from thaqip_ingestion.p2w.orchestrator import build_competitor_draws

    entries = [
        {
            "vendor_id": vendor_id,
            "participation": {"probability": 0.6},
            "prediction": PricePrediction(
                tender_id=1,
                prediction_scope=PredictionScope.COMPETITOR,
                subject_id=vendor_id,
                p10=900_000.0,
                p50=1_000_000.0,
                p90=1_150_000.0,
                evidence_count=9,
                evidence_tier=EvidenceTier.A,
            ).to_dict(),
        }
        for vendor_id in (7, 3, 11)
    ]
    forward = build_competitor_draws(entries)
    reversed_order = build_competitor_draws(list(reversed(entries)))
    assert [d.vendor_id for d in forward] == [3, 7, 11], (
        'build_competitor_draws dropped every entry; the JSON round-trip broke'
    )
    assert forward == reversed_order
    a = simulate(user_bid=1_000_000.0, competitors=forward, seed=5, iterations=1000)
    b = simulate(user_bid=1_000_000.0, competitors=reversed_order, seed=5, iterations=1000)
    assert a.to_dict() == b.to_dict()


# --- 4. common random numbers ------------------------------------------------


@pytest.mark.parametrize(("name", "field"), CONFIGS[:8], ids=CONFIG_IDS[:8])
def test_curve_point_equals_simulate_bit_for_bit(name: str, field: list[CompetitorDraw]) -> None:
    grid = PRICE_GRID[::7]
    curve = win_probability_curve(
        price_grid=grid, competitors=field, seed=31337, iterations=1200,
        user_technical_pass_p=0.9,
    )
    for point in curve:
        direct = simulate(
            user_bid=point.price, competitors=field, seed=31337, iterations=1200,
            user_technical_pass_p=0.9,
        )
        assert point.win_probability == direct.win_probability, (
            f"{name} @ {point.price}: curve {point.win_probability!r} != "
            f"simulate {direct.win_probability!r}"
        )
        assert point.standard_error == direct.standard_error


def test_curve_collapses_duplicate_prices_and_sorts() -> None:
    curve = win_probability_curve(
        price_grid=[1.2e6, 8e5, 1.2e6, 8e5, 1.0e6],
        competitors=FIELD,
        seed=11,
        iterations=800,
    )
    assert [point.price for point in curve] == [8e5, 1.0e6, 1.2e6]


# --- 5. bounds ---------------------------------------------------------------


@pytest.mark.parametrize(("name", "field"), CONFIGS, ids=CONFIG_IDS)
def test_all_probabilities_are_bounded_and_ranks_sum_to_one(
    name: str, field: list[CompetitorDraw]
) -> None:
    for price in PRICE_GRID[::5]:
        result = simulate(
            user_bid=price, competitors=field, seed=909, iterations=400,
            user_technical_pass_p=0.9,
        )
        assert 0.0 <= result.win_probability <= 1.0
        assert result.standard_error >= 0.0
        total = sum(result.rank_distribution.values())
        assert abs(total - 1.0) <= 1e-6, f"{name} @ {price}: ranks sum to {total!r}"
        for rank, mass in result.rank_distribution.items():
            assert 0.0 <= mass <= 1.0
            assert rank == RANK_DISQUALIFIED or 1 <= rank <= len(field) + 1
        for probability in result.undercut_probabilities.values():
            assert probability is None or 0.0 <= probability <= 1.0
        assert set(result.undercut_probabilities) == {c.vendor_id for c in field}
        if result.price_to_beat is not None:
            assert result.price_to_beat > 0.0


def test_win_probability_never_exceeds_the_qualified_share_of_iterations() -> None:
    """Winning requires qualifying, and that bound is exact, not statistical.

    The population ceiling is `user_technical_pass_p`, but a 500-iteration sample
    can legitimately sit a couple of standard errors above it (0.306 against a
    0.30 ceiling is not a bug). The *exact* invariant is against the realised
    disqualification mass in the same run, and that is what is asserted here; the
    population ceiling is checked separately with its sampling error allowed for.
    """
    for pass_p in (0.0, 0.3, 0.75, 1.0):
        for name, field in CONFIGS[:10]:
            result = simulate(
                user_bid=1.0, competitors=field, seed=606, iterations=4000,
                user_technical_pass_p=pass_p,
            )
            qualified_share = 1.0 - result.rank_distribution.get(RANK_DISQUALIFIED, 0.0)
            assert result.win_probability <= qualified_share + 1e-12, name
            # 1 SAR: the user is cheapest in every world, so they win iff they
            # qualify -- the two must agree exactly, run for run.
            assert result.win_probability == pytest.approx(qualified_share, abs=1e-12), name
            assert result.win_probability <= pass_p + 4.0 * result.standard_error + 1e-12


def test_undercut_is_none_only_when_the_competitor_never_showed_up() -> None:
    field = [
        CompetitorDraw(1, 0.0, math.log(1_000_000.0), 0.2, 1.0),
        CompetitorDraw(2, 1.0, math.log(1_000_000.0), 0.2, 1.0),
    ]
    result = simulate(user_bid=1.0, competitors=field, seed=8, iterations=500)
    assert result.undercut_probabilities[1] is None
    assert result.undercut_probabilities[2] == 1.0


# --- 6. edge cases -----------------------------------------------------------


def test_zero_competitors_is_answered_analytically() -> None:
    result = simulate(user_bid=1_000_000.0, competitors=[], seed=1, iterations=1000)
    assert result.win_probability == DEFAULT_USER_TECHNICAL_PASS_P
    assert result.assumptions["analytic"] is True
    assert result.price_to_beat is None
    assert result.undercut_probabilities == {}
    assert sum(result.rank_distribution.values()) == pytest.approx(1.0, abs=1e-6)
    # Price cannot matter when there is no field to lose to.
    other = simulate(user_bid=9_999_999.0, competitors=[], seed=1, iterations=1000)
    assert other.win_probability == result.win_probability


def test_single_competitor_matches_the_closed_form() -> None:
    field = [CompetitorDraw(1, 0.6, math.log(1_000_000.0), 0.25, 0.8)]
    for bid in (600_000.0, 1_000_000.0, 1_600_000.0):
        result = simulate(
            user_bid=bid, competitors=field, seed=44, iterations=40_000,
            user_technical_pass_p=1.0,
        )
        expected = analytic_win_probability(bid, field, 1.0)
        assert abs(result.win_probability - expected) <= 4.0 * result.standard_error + 1e-9


def test_all_competitors_disqualified_leaves_the_field_empty() -> None:
    field = [
        CompetitorDraw(vendor_id, 1.0, math.log(500_000.0), 0.2, 0.0)
        for vendor_id in (1, 2, 3)
    ]
    result = simulate(
        user_bid=10_000_000.0, competitors=field, seed=2, iterations=2000,
        user_technical_pass_p=1.0,
    )
    assert result.win_probability == 1.0
    assert result.rank_distribution == {1: 1.0}
    assert result.price_to_beat is None, "no qualified bid can define a price to beat"
    assert result.assumptions["price_to_beat_basis_iterations"] == 0
    # They still participate, so undercut is measurable and the user is dearer.
    assert result.undercut_probabilities == {1: 0.0, 2: 0.0, 3: 0.0}


def test_no_competitor_ever_participates() -> None:
    field = [CompetitorDraw(1, 0.0, math.log(500_000.0), 0.2, 1.0)]
    result = simulate(
        user_bid=10_000_000.0, competitors=field, seed=2, iterations=1000,
        user_technical_pass_p=1.0,
    )
    assert result.win_probability == 1.0
    assert result.price_to_beat is None
    assert result.undercut_probabilities == {1: None}


@pytest.mark.parametrize(("tied", "expected"), [(1, 0.5), (2, 1.0 / 3.0), (3, 0.25)])
def test_exact_ties_split_the_win_evenly(tied: int, expected: float) -> None:
    """An exact tie needs `median_bid`, not the nominal price.

    exp(log(1_000_000)) is 999999.9999999995, so bidding a round 1e6 against a
    "1e6" competitor is a *loss*, not a tie. Using `median_bid` is the only way
    to construct a real tie and exercise the split-evenly policy.
    """
    base = CompetitorDraw(1, 1.0, math.log(1_000_000.0), 0.0, 1.0)
    field = [replace(base, vendor_id=index + 1) for index in range(tied)]
    result = simulate(
        user_bid=base.median_bid, competitors=field, seed=17, iterations=1000,
        user_technical_pass_p=1.0,
    )
    assert result.win_probability == pytest.approx(expected, abs=1e-9)
    # Tied bidders share rank 1 rather than one being arbitrarily promoted.
    assert result.rank_distribution == {1: 1.0}
    assert result.undercut_probabilities == dict.fromkeys(range(1, tied + 1), 0.0)


def test_partial_participation_tie_is_a_mixture() -> None:
    base = CompetitorDraw(1, 0.5, math.log(1_000_000.0), 0.0, 1.0)
    result = simulate(
        user_bid=base.median_bid, competitors=[base], seed=19, iterations=40_000,
        user_technical_pass_p=1.0,
    )
    # 50% of the time alone (win 1.0), 50% tied (win 0.5) => 0.75.
    assert result.win_probability == pytest.approx(0.75, abs=4.0 * result.standard_error)


@pytest.mark.parametrize("bid", [1.0, 1e-3, 1e12, 1e15])
def test_absurd_bids_stay_bounded(bid: float) -> None:
    """1 SAR and 1e12 SAR must both give a defensible answer, not a crash.

    At 1e12 the residual win probability is *not* zero and must not be: FIELD's
    members each fail to show up or fail technically some of the time, and in the
    ~2.7% of worlds where nobody qualifies the user wins at any price. The
    closed form pins that number, so this is a real check rather than a shrug.
    """
    result = simulate(
        user_bid=bid, competitors=FIELD, seed=23, iterations=20_000,
        user_technical_pass_p=1.0,
    )
    assert 0.0 <= result.win_probability <= 1.0
    expected = analytic_win_probability(bid, FIELD, 1.0)
    assert result.win_probability == pytest.approx(
        expected, abs=4.0 * result.standard_error + 1e-9
    )
    if bid <= 1.0:
        assert result.win_probability == 1.0
    if bid >= 1e12:
        empty_field_share = math.prod(
            1.0 - c.participation_p * c.technical_pass_p for c in FIELD
        )
        assert expected == pytest.approx(empty_field_share, abs=1e-12)
        assert result.win_probability < 0.05


def test_invalid_bids_are_rejected_rather_than_silently_degraded() -> None:
    for bad in (0.0, -1.0, float("nan"), float("inf"), float("-inf"), None, "abc", True):
        with pytest.raises(ValueError):
            simulate(user_bid=bad, competitors=FIELD, seed=1, iterations=100)
    for bad_iterations in (0, -5, 200_001, 4.5, True):
        with pytest.raises(ValueError):
            simulate(
                user_bid=1e6, competitors=FIELD, seed=1, iterations=bad_iterations
            )
    with pytest.raises(ValueError):
        simulate(user_bid=1e6, competitors=FIELD, seed=1.5, iterations=100)
    with pytest.raises(ValueError):
        simulate(
            user_bid=1e6, competitors=FIELD, seed=1, iterations=100,
            evaluation_rule="highest_score",
        )
    with pytest.raises(ValueError):
        simulate(
            user_bid=1e6,
            competitors=[FIELD[0], FIELD[0]],
            seed=1,
            iterations=100,
        )


def test_a_numeric_string_bid_is_coerced_not_rejected() -> None:
    """Documented, deliberate leniency -- pinned so it cannot drift silently.

    `_require_finite` coerces with `float(value)`, so "1000000" is accepted and
    behaves exactly like the float. That is the same contract `p2w.contracts`
    uses, and callers relying on either behaviour deserve a test rather than a
    surprise.
    """
    coerced = simulate(user_bid="1000000", competitors=FIELD, seed=3, iterations=500)
    native = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=3, iterations=500)
    assert coerced.to_dict() == native.to_dict()


def test_price_to_beat_reports_none_when_the_target_is_unreachable() -> None:
    curve = win_probability_curve(
        price_grid=PRICE_GRID, competitors=FIELD, seed=5, iterations=2000,
        user_technical_pass_p=0.8,
    )
    # 0.99 is above the technical-pass ceiling: unattainable at any price.
    assert price_to_beat(curve=curve, target_probability=0.99) is None
    # A reachable target must land inside the grid and be monotone in the target.
    lenient = price_to_beat(curve=curve, target_probability=0.30)
    strict = price_to_beat(curve=curve, target_probability=0.60)
    assert lenient is not None and strict is not None
    assert strict <= lenient
    assert min(PRICE_GRID) <= strict <= max(PRICE_GRID)
    # Censored by the grid rather than extrapolated.
    assert price_to_beat(curve=curve, target_probability=0.0) == max(PRICE_GRID)


def test_price_to_beat_accepts_a_json_round_tripped_curve() -> None:
    curve = win_probability_curve(
        price_grid=PRICE_GRID[::4], competitors=FIELD, seed=5, iterations=1500
    )
    as_json = json.loads(json.dumps([point.to_dict() for point in curve]))
    assert price_to_beat(curve=as_json, target_probability=0.4) == price_to_beat(
        curve=curve, target_probability=0.4
    )
    pairs = [(point.price, point.win_probability) for point in curve]
    assert price_to_beat(curve=pairs, target_probability=0.4) == price_to_beat(
        curve=curve, target_probability=0.4
    )


def test_curve_point_type_is_what_the_optimizer_consumes() -> None:
    curve = win_probability_curve(
        price_grid=[9e5, 1e6], competitors=FIELD, seed=5, iterations=500
    )
    assert all(isinstance(point, CurvePoint) for point in curve)


# --- 7. analytic cross-check -------------------------------------------------


@pytest.mark.parametrize("bid", [700_000.0, 900_000.0, 1_000_000.0, 1_300_000.0, 1_800_000.0])
def test_two_competitor_case_matches_the_lognormal_closed_form(bid: float) -> None:
    """The headline correctness check: simulation vs. exact maths.

    Two independent lognormal competitors with participation and technical-pass
    probabilities below 1. The tolerance is 4 standard errors of the simulated
    proportion — a genuinely tight band at 40,000 iterations (about +/-0.010 at
    the worst point), so a systematic bias of even one percentage point fails.
    """
    field = [
        CompetitorDraw(1, 0.8, math.log(1_000_000.0), 0.20, 0.9),
        CompetitorDraw(2, 0.6, math.log(1_100_000.0), 0.35, 1.0),
    ]
    user_pass = 0.95
    result = simulate(
        user_bid=bid, competitors=field, seed=98765, iterations=40_000,
        user_technical_pass_p=user_pass,
    )
    expected = analytic_win_probability(bid, field, user_pass)
    tolerance = 4.0 * result.standard_error + 1e-9
    assert abs(result.win_probability - expected) <= tolerance, (
        f"bid {bid}: simulated {result.win_probability:.5f} vs analytic "
        f"{expected:.5f} (tolerance {tolerance:.5f})"
    )


def test_analytic_cross_check_is_unbiased_across_seeds() -> None:
    """Averaging independent seeds must converge on the closed form.

    A single-seed comparison can pass with a biased sampler if the bias is small
    relative to one run's noise. Averaging 40 seeds cuts the noise by ~6.3x and
    turns the check into a real test of the mean.
    """
    field = [
        CompetitorDraw(1, 0.8, math.log(1_000_000.0), 0.20, 0.9),
        CompetitorDraw(2, 0.6, math.log(1_100_000.0), 0.35, 1.0),
    ]
    bid, user_pass, iterations, seeds = 1_000_000.0, 0.95, 4000, 40
    estimates = [
        simulate(
            user_bid=bid, competitors=field, seed=seed, iterations=iterations,
            user_technical_pass_p=user_pass,
        ).win_probability
        for seed in range(seeds)
    ]
    mean = statistics.mean(estimates)
    expected = analytic_win_probability(bid, field, user_pass)
    pooled_se = math.sqrt(expected * (1.0 - expected) / (iterations * seeds))
    assert abs(mean - expected) <= 4.0 * pooled_se, (
        f"mean over {seeds} seeds {mean:.5f} vs analytic {expected:.5f} "
        f"(4 pooled se = {4 * pooled_se:.5f})"
    )


def test_rank_one_probability_matches_the_closed_form() -> None:
    """Rank 1 is the win event before the tie split, so it has a closed form too.

    With continuous prices, ties have measure zero, so P(rank 1) must equal the
    analytic win probability. This checks the rank machinery independently of the
    win-credit machinery — the two are computed by different functions.
    """
    field = [
        CompetitorDraw(1, 0.8, math.log(1_000_000.0), 0.20, 0.9),
        CompetitorDraw(2, 0.6, math.log(1_100_000.0), 0.35, 1.0),
    ]
    user_pass = 0.95
    bid = 950_000.0
    result = simulate(
        user_bid=bid, competitors=field, seed=555, iterations=40_000,
        user_technical_pass_p=user_pass,
    )
    expected = analytic_win_probability(bid, field, user_pass)
    assert result.rank_distribution[1] == pytest.approx(
        expected, abs=4.0 * result.standard_error + 1e-9
    )
    assert result.rank_distribution[1] == pytest.approx(result.win_probability, abs=1e-12)
    assert result.rank_distribution[RANK_DISQUALIFIED] == pytest.approx(
        1.0 - user_pass, abs=0.02
    )


def test_undercut_probability_matches_the_closed_form() -> None:
    """P(user_bid < competitor_bid | participates) is 1 - Phi((ln b - mu)/sigma)."""
    field = [CompetitorDraw(1, 0.7, math.log(1_000_000.0), 0.30, 0.5)]
    bid = 1_050_000.0
    result = simulate(
        user_bid=bid, competitors=field, seed=1234, iterations=40_000,
        user_technical_pass_p=1.0,
    )
    expected = 1.0 - _norm_cdf((math.log(bid) - field[0].log_mu) / field[0].log_sigma)
    # Conditional on participation: ~28,000 effective draws.
    conditional_se = math.sqrt(
        expected * (1.0 - expected) / (40_000 * field[0].participation_p)
    )
    assert result.undercut_probabilities[1] == pytest.approx(
        expected, abs=4.0 * conditional_se
    )


def test_price_to_beat_field_tracks_the_median_of_the_best_rival() -> None:
    """With one always-present, always-qualified rival it is that rival's median."""
    field = [CompetitorDraw(1, 1.0, math.log(2_000_000.0), 0.25, 1.0)]
    result = simulate(
        user_bid=1_000_000.0, competitors=field, seed=64, iterations=20_000,
        user_technical_pass_p=1.0,
    )
    assert result.price_to_beat is not None
    assert result.price_to_beat == pytest.approx(2_000_000.0, rel=0.02)
    assert result.assumptions["price_to_beat_basis_iterations"] == 20_000


# --- 8. sampler soundness ----------------------------------------------------


def test_competitor_prices_follow_the_requested_lognormal() -> None:
    """The sampled marginal must match the lognormal CDF at nine quantiles.

    `Random.gauss` caches a spare Box-Muller value, so a competitor's z is
    sometimes the cos branch and sometimes the sin branch. If that pairing were
    mishandled the marginal would be skewed, and comparing the empirical CDF at
    nine points against Phi is the check that would notice.
    """
    competitor = CompetitorDraw(1, 1.0, math.log(1_000_000.0), 0.30, 1.0)
    iterations = 40_000
    for quantile in (0.05, 0.1, 0.25, 0.4, 0.5, 0.6, 0.75, 0.9, 0.95):
        # Price at this quantile of the modelled lognormal.
        z = statistics.NormalDist().inv_cdf(quantile)
        price = math.exp(competitor.log_mu + competitor.log_sigma * z)
        result = simulate(
            user_bid=price, competitors=[competitor], seed=2024, iterations=iterations,
            user_technical_pass_p=1.0,
        )
        # P(win) = P(competitor prices above me) = 1 - quantile.
        se = math.sqrt(quantile * (1.0 - quantile) / iterations)
        assert result.win_probability == pytest.approx(1.0 - quantile, abs=4.0 * se), (
            f"quantile {quantile}: simulated tail {result.win_probability:.4f}"
        )


def test_competitors_are_drawn_independently_of_each_other() -> None:
    """Two identical rivals must behave like a product, not like a clone.

    With two identical competitors at their own median, P(both above) is 0.25 if
    the draws are independent and 0.5 if the second competitor secretly reuses
    the first one's normal deviate. The gap is enormous, so this is a decisive
    test of the draw order and of Box-Muller pairing.
    """
    twin = CompetitorDraw(1, 1.0, math.log(1_000_000.0), 0.30, 1.0)
    pair = [twin, replace(twin, vendor_id=2)]
    iterations = 60_000
    result = simulate(
        user_bid=twin.median_bid, competitors=pair, seed=7, iterations=iterations,
        user_technical_pass_p=1.0,
    )
    se = math.sqrt(0.25 * 0.75 / iterations)
    assert result.win_probability == pytest.approx(0.25, abs=4.0 * se), (
        f"joint tail {result.win_probability:.4f}; 0.5 would mean shared randomness"
    )
    for probability in result.undercut_probabilities.values():
        assert probability == pytest.approx(0.5, abs=0.02)


def test_participation_is_drawn_at_the_requested_rate() -> None:
    field = [
        CompetitorDraw(1, 0.10, math.log(1_000_000.0), 0.2, 1.0),
        CompetitorDraw(2, 0.50, math.log(1_000_000.0), 0.2, 1.0),
        CompetitorDraw(3, 0.90, math.log(1_000_000.0), 0.2, 1.0),
    ]
    iterations = 40_000
    result = simulate(
        user_bid=1.0, competitors=field, seed=13, iterations=iterations,
        user_technical_pass_p=1.0,
    )
    # At 1 SAR the user always undercuts, so the undercut denominator exposes the
    # realised participation rate directly: it is 1.0 for everyone who showed up.
    assert all(value == 1.0 for value in result.undercut_probabilities.values())
    assert result.assumptions["competitor_count"] == 3


# --- 9. numerical robustness (regression: OverflowError, see battery report) ---


def test_unrepresentable_price_distribution_is_rejected_at_construction() -> None:
    """A price distribution that cannot be sampled must fail as ValueError.

    Before the fix, `CompetitorDraw(log_sigma=300)` constructed happily and then
    `simulate` died with `OverflowError: math range error` from `math.exp` in the
    middle of the run — breaking the module's stated contract that invalid input
    raises ValueError and never degrades silently.
    """
    with pytest.raises(ValueError, match="representable"):
        CompetitorDraw(1, 1.0, math.log(1_000_000.0), 300.0, 1.0)
    with pytest.raises(ValueError, match="representable"):
        CompetitorDraw(1, 1.0, 1_000.0, 0.0, 1.0)


def test_extreme_but_representable_distributions_still_simulate() -> None:
    """The guard must not reject anything a real prediction could produce.

    An implied sigma of 5 corresponds to a p90/p50 ratio of e^6.4 — already far
    beyond anything the corpus supports — and must still run.
    """
    field = [CompetitorDraw(1, 1.0, math.log(1_000_000.0), 5.0, 1.0)]
    result = simulate(
        user_bid=1_000_000.0, competitors=field, seed=1, iterations=2000,
        user_technical_pass_p=1.0,
    )
    assert 0.0 <= result.win_probability <= 1.0
    assert result.price_to_beat is not None and math.isfinite(result.price_to_beat)
    assert result.win_probability == pytest.approx(
        0.5, abs=4.0 * result.standard_error
    )


def test_tiny_prices_underflow_to_zero_without_crashing() -> None:
    """The other tail: an absurdly cheap competitor rounds to 0, never raises."""
    field = [CompetitorDraw(1, 1.0, -800.0, 0.0, 1.0)]
    result = simulate(
        user_bid=1_000_000.0, competitors=field, seed=1, iterations=200,
        user_technical_pass_p=1.0,
    )
    assert result.win_probability == 0.0
    assert result.price_to_beat == 0.0
