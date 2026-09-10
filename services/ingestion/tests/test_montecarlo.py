"""Tests for the P2W Monte Carlo win-probability engine.

Every test is deterministic: seeds are explicit, there is no sleeping, no
network and no database. The properties under test are the ones a user would
notice if they broke — monotonicity, reproducibility, bounded probabilities and
the tie convention — rather than the exact value of any one simulated number.
"""
from __future__ import annotations

import itertools
import math

import pytest

from thaqip_ingestion.p2w.contracts import MODEL_VERSION
from thaqip_ingestion.p2w.montecarlo import (
    DEFAULT_CURVE_ITERATIONS,
    DEFAULT_ITERATIONS,
    DEFAULT_USER_TECHNICAL_PASS_P,
    MAX_ITERATIONS,
    MAX_STANDARD_ERROR,
    RANK_DISQUALIFIED,
    CompetitorDraw,
    CurvePoint,
    SimulationOutput,
    price_to_beat,
    simulate,
    standard_error_of_proportion,
    win_probability_curve,
)

SEED = 20260910
# A plausible mid-size Saudi tender: median bid 1,000,000 SAR.
LOG_MU = math.log(1_000_000.0)
LOG_SIGMA = 0.20


def competitor(
    vendor_id: int,
    *,
    participation_p: float = 1.0,
    log_mu: float = LOG_MU,
    log_sigma: float = LOG_SIGMA,
    technical_pass_p: float = 1.0,
) -> CompetitorDraw:
    return CompetitorDraw(
        vendor_id=vendor_id,
        participation_p=participation_p,
        log_mu=log_mu,
        log_sigma=log_sigma,
        technical_pass_p=technical_pass_p,
    )


FIELD = [competitor(1), competitor(2, log_mu=LOG_MU + 0.05), competitor(3, participation_p=0.6)]


# --- Input validation --------------------------------------------------------


class TestCompetitorDrawValidation:
    def test_valid_draw_exposes_median_bid(self) -> None:
        c = competitor(7)
        assert c.median_bid == pytest.approx(1_000_000.0)
        assert c.to_dict()["vendor_id"] == 7

    @pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf")])
    def test_participation_probability_must_be_a_probability(self, value: float) -> None:
        with pytest.raises(ValueError, match="participation_p"):
            competitor(1, participation_p=value)

    @pytest.mark.parametrize("value", [-0.5, 2.0])
    def test_technical_pass_probability_must_be_a_probability(self, value: float) -> None:
        with pytest.raises(ValueError, match="technical_pass_p"):
            competitor(1, technical_pass_p=value)

    def test_negative_sigma_rejected(self) -> None:
        with pytest.raises(ValueError, match="log_sigma"):
            competitor(1, log_sigma=-0.1)

    def test_zero_sigma_allowed_and_deterministic(self) -> None:
        c = competitor(1, log_sigma=0.0)
        assert c.median_bid == math.exp(LOG_MU)

    def test_non_finite_log_mu_rejected(self) -> None:
        with pytest.raises(ValueError, match="log_mu"):
            competitor(1, log_mu=float("inf"))

    def test_bool_is_not_a_number(self) -> None:
        with pytest.raises(ValueError, match="log_mu"):
            competitor(1, log_mu=True)  # type: ignore[arg-type]


class TestSimulateValidation:
    def test_non_positive_bid_rejected(self) -> None:
        with pytest.raises(ValueError, match="user_bid"):
            simulate(user_bid=0.0, competitors=FIELD, seed=SEED)

    def test_duplicate_vendor_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate vendor_id"):
            simulate(user_bid=1e6, competitors=[competitor(1), competitor(1)], seed=SEED)

    def test_unknown_evaluation_rule_rejected(self) -> None:
        with pytest.raises(ValueError, match="evaluation_rule"):
            simulate(
                user_bid=1e6, competitors=FIELD, seed=SEED, evaluation_rule="highest_score"
            )

    @pytest.mark.parametrize("iterations", [0, -5, MAX_ITERATIONS + 1])
    def test_iteration_bounds_enforced(self, iterations: int) -> None:
        with pytest.raises(ValueError, match="iterations"):
            simulate(user_bid=1e6, competitors=FIELD, seed=SEED, iterations=iterations)

    def test_non_integer_seed_rejected(self) -> None:
        with pytest.raises(ValueError, match="seed"):
            simulate(user_bid=1e6, competitors=FIELD, seed="abc")  # type: ignore[arg-type]

    def test_non_integer_iterations_rejected(self) -> None:
        with pytest.raises(ValueError, match="iterations"):
            simulate(user_bid=1e6, competitors=FIELD, seed=SEED, iterations=100.0)  # type: ignore[arg-type]

    def test_wrong_competitor_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="CompetitorDraw"):
            simulate(user_bid=1e6, competitors=[{"vendor_id": 1}], seed=SEED)  # type: ignore[list-item]

    def test_user_technical_pass_must_be_probability(self) -> None:
        with pytest.raises(ValueError, match="user_technical_pass_p"):
            simulate(user_bid=1e6, competitors=FIELD, seed=SEED, user_technical_pass_p=1.5)


# --- Determinism -------------------------------------------------------------


class TestDeterminism:
    def test_same_seed_reproduces_identical_output(self) -> None:
        a = simulate(user_bid=950_000.0, competitors=FIELD, seed=SEED, iterations=3000)
        b = simulate(user_bid=950_000.0, competitors=FIELD, seed=SEED, iterations=3000)
        assert a == b
        assert a.to_dict() == b.to_dict()

    def test_output_carries_no_timestamp(self) -> None:
        """Determinism means the output object cannot contain a clock reading."""
        result = simulate(user_bid=950_000.0, competitors=FIELD, seed=SEED, iterations=500)
        assert "generated_at" not in result.to_dict()

    def test_different_seed_differs_but_stays_close(self) -> None:
        a = simulate(user_bid=950_000.0, competitors=FIELD, seed=SEED, iterations=DEFAULT_ITERATIONS)
        b = simulate(
            user_bid=950_000.0, competitors=FIELD, seed=SEED + 1, iterations=DEFAULT_ITERATIONS
        )
        assert a.win_probability != b.win_probability
        # Within ~4 standard errors of each other: different noise, same answer.
        assert abs(a.win_probability - b.win_probability) < 4.0 * a.standard_error + 0.005

    def test_competitor_order_is_part_of_the_seeded_stream(self) -> None:
        """Reordering the field changes which random numbers each competitor gets.

        Documented, not accidental: the draw order is fixed by the input order,
        so callers must keep a stable order for a stable feature snapshot.
        """
        forward = simulate(user_bid=950_000.0, competitors=FIELD, seed=SEED, iterations=2000)
        reverse = simulate(
            user_bid=950_000.0, competitors=list(reversed(FIELD)), seed=SEED, iterations=2000
        )
        assert forward.win_probability != reverse.win_probability
        assert abs(forward.win_probability - reverse.win_probability) < 0.06

    def test_curve_and_simulate_share_one_sampler(self) -> None:
        price = 980_000.0
        curve = win_probability_curve(
            price_grid=[900_000.0, price, 1_100_000.0],
            competitors=FIELD,
            seed=SEED,
            iterations=1500,
        )
        direct = simulate(user_bid=price, competitors=FIELD, seed=SEED, iterations=1500)
        point = next(p for p in curve if p.price == price)
        assert point.win_probability == direct.win_probability


# --- Bounds and shape --------------------------------------------------------


class TestBounds:
    def test_probabilities_are_bounded(self) -> None:
        result = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=2000)
        assert 0.0 <= result.win_probability <= 1.0
        for probability in result.rank_distribution.values():
            assert 0.0 <= probability <= 1.0
        for probability in result.undercut_probabilities.values():
            assert probability is None or 0.0 <= probability <= 1.0

    def test_rank_distribution_sums_to_one(self) -> None:
        result = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=2000)
        assert sum(result.rank_distribution.values()) == pytest.approx(1.0)

    def test_rank_never_exceeds_field_size_plus_one(self) -> None:
        result = simulate(user_bid=2_000_000.0, competitors=FIELD, seed=SEED, iterations=2000)
        assert max(result.rank_distribution) <= len(FIELD) + 1
        assert min(result.rank_distribution) >= RANK_DISQUALIFIED

    def test_disqualification_mass_matches_user_technical_pass(self) -> None:
        result = simulate(
            user_bid=1_000_000.0,
            competitors=FIELD,
            seed=SEED,
            iterations=DEFAULT_ITERATIONS,
            user_technical_pass_p=0.80,
        )
        assert result.rank_distribution[RANK_DISQUALIFIED] == pytest.approx(0.20, abs=0.02)

    def test_win_probability_never_exceeds_user_technical_pass(self) -> None:
        for bid in (100.0, 500_000.0, 1_000_000.0, 5_000_000.0):
            result = simulate(
                user_bid=bid,
                competitors=FIELD,
                seed=SEED,
                iterations=1500,
                user_technical_pass_p=0.9,
            )
            # Sampling noise can push the empirical rate a hair over the true
            # ceiling; allow ~4 standard errors rather than an unphysical bound.
            assert result.win_probability <= 0.9 + 4.0 * result.standard_error

    def test_assumptions_are_reported(self) -> None:
        result = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=500)
        assumptions = result.assumptions
        assert assumptions["model_version"] == MODEL_VERSION
        assert assumptions["evaluation_rule"] == "lowest_qualified"
        assert assumptions["tie_policy"] == "split_evenly"
        assert assumptions["competitor_count"] == len(FIELD)
        assert assumptions["price_distribution"] == "lognormal"
        assert assumptions["independent_competitors"] is True

    def test_to_dict_is_json_shaped(self) -> None:
        import json

        result = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=200)
        payload = json.loads(json.dumps(result.to_dict()))
        assert payload["iterations"] == 200
        assert payload["seed"] == SEED
        assert set(payload["undercut_probabilities"]) == {"1", "2", "3"}


# --- Monotonicity ------------------------------------------------------------


class TestMonotonicity:
    def test_lowering_the_bid_never_lowers_win_probability(self) -> None:
        prices = [700_000.0 + 50_000.0 * step for step in range(14)]
        probabilities = [
            simulate(user_bid=price, competitors=FIELD, seed=SEED, iterations=2000).win_probability
            for price in prices
        ]
        for earlier, later in itertools.pairwise(probabilities):
            assert later <= earlier + 1e-12

    def test_curve_is_monotone_non_increasing(self) -> None:
        grid = [600_000.0 + 25_000.0 * step for step in range(40)]
        curve = win_probability_curve(price_grid=grid, competitors=FIELD, seed=SEED)
        for earlier, later in itertools.pairwise(curve):
            assert later.win_probability <= earlier.win_probability + 1e-12

    def test_curve_actually_moves_across_a_wide_grid(self) -> None:
        """A monotone-but-flat curve would pass the monotonicity test vacuously."""
        curve = win_probability_curve(
            price_grid=[300_000.0, 1_000_000.0, 3_000_000.0], competitors=FIELD, seed=SEED
        )
        assert curve[0].win_probability > 0.9
        assert curve[-1].win_probability < 0.02

    def test_curve_prices_sorted_and_deduplicated(self) -> None:
        curve = win_probability_curve(
            price_grid=[1_000_000.0, 800_000.0, 1_000_000.0, 900_000.0],
            competitors=FIELD,
            seed=SEED,
            iterations=500,
        )
        assert [point.price for point in curve] == [800_000.0, 900_000.0, 1_000_000.0]

    def test_empty_price_grid_rejected(self) -> None:
        with pytest.raises(ValueError, match="price_grid"):
            win_probability_curve(price_grid=[], competitors=FIELD, seed=SEED)

    def test_curve_default_iterations(self) -> None:
        curve = win_probability_curve(price_grid=[1_000_000.0], competitors=FIELD, seed=SEED)
        expected = standard_error_of_proportion(curve[0].win_probability, DEFAULT_CURVE_ITERATIONS)
        assert curve[0].standard_error == pytest.approx(expected)


# --- Convergence -------------------------------------------------------------


class TestConvergence:
    def test_standard_error_matches_the_binomial_formula(self) -> None:
        result = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=2500)
        p = result.win_probability
        assert result.standard_error == pytest.approx(math.sqrt(p * (1 - p) / 2500))

    def test_standard_error_shrinks_like_one_over_sqrt_n(self) -> None:
        errors = []
        for iterations in (1000, 4000, 16000):
            result = simulate(
                user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=iterations
            )
            errors.append(result.standard_error)
        # Quadrupling n should roughly halve the standard error.
        assert errors[1] == pytest.approx(errors[0] / 2.0, rel=0.10)
        assert errors[2] == pytest.approx(errors[1] / 2.0, rel=0.10)

    def test_convergence_flag_follows_the_threshold(self) -> None:
        tiny = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=100)
        assert tiny.standard_error > MAX_STANDARD_ERROR
        assert tiny.convergence_ok is False

        big = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=20_000)
        assert big.standard_error <= MAX_STANDARD_ERROR
        assert big.convergence_ok is True

    def test_default_iterations_converge_at_any_probability(self) -> None:
        """sqrt(0.25/DEFAULT_ITERATIONS) is the worst case and must clear the bar."""
        worst = standard_error_of_proportion(0.5, DEFAULT_ITERATIONS)
        assert worst <= MAX_STANDARD_ERROR

    def test_standard_error_helper_validates(self) -> None:
        with pytest.raises(ValueError, match="probability"):
            standard_error_of_proportion(1.5, 100)
        with pytest.raises(ValueError, match="iterations"):
            standard_error_of_proportion(0.5, 0)

    def test_simulation_is_close_to_the_analytic_two_bidder_answer(self) -> None:
        """One deterministic competitor: the answer is known in closed form."""
        rival = competitor(1, log_sigma=0.0, log_mu=math.log(1_000_000.0))
        result = simulate(
            user_bid=999_999.0,
            competitors=[rival],
            seed=SEED,
            iterations=DEFAULT_ITERATIONS,
            user_technical_pass_p=0.9,
        )
        # User is always cheaper, so wins exactly when technically qualified.
        assert result.win_probability == pytest.approx(0.9, abs=0.02)


# --- Edge cases --------------------------------------------------------------


class TestEdgeCases:
    def test_zero_competitors_gives_exactly_the_technical_pass_probability(self) -> None:
        for p in (0.0, 0.5, 0.95, 1.0):
            result = simulate(
                user_bid=1_000_000.0,
                competitors=[],
                seed=SEED,
                iterations=1000,
                user_technical_pass_p=p,
            )
            assert result.win_probability == p
            assert result.rank_distribution == {RANK_DISQUALIFIED: 1.0 - p, 1: p}
            assert result.undercut_probabilities == {}
            assert result.price_to_beat is None
            assert result.assumptions["analytic"] is True

    def test_zero_competitors_default_pass_probability(self) -> None:
        result = simulate(user_bid=1_000_000.0, competitors=[], seed=SEED, iterations=1000)
        assert result.win_probability == DEFAULT_USER_TECHNICAL_PASS_P

    def test_zero_competitor_curve_is_flat(self) -> None:
        curve = win_probability_curve(
            price_grid=[1.0, 1_000_000.0, 9_000_000.0], competitors=[], seed=SEED, iterations=500
        )
        assert {point.win_probability for point in curve} == {DEFAULT_USER_TECHNICAL_PASS_P}

    def test_single_competitor_splits_the_field(self) -> None:
        rival = competitor(1, log_sigma=0.0)
        cheap = simulate(
            user_bid=rival.median_bid * 0.9,
            competitors=[rival],
            seed=SEED,
            iterations=2000,
            user_technical_pass_p=1.0,
        )
        dear = simulate(
            user_bid=rival.median_bid * 1.1,
            competitors=[rival],
            seed=SEED,
            iterations=2000,
            user_technical_pass_p=1.0,
        )
        assert cheap.win_probability == 1.0
        assert dear.win_probability == 0.0
        assert cheap.rank_distribution == {1: 1.0}
        assert dear.rank_distribution == {2: 1.0}

    def test_all_competitors_technically_disqualified(self) -> None:
        field = [competitor(i, technical_pass_p=0.0) for i in (1, 2, 3)]
        result = simulate(
            user_bid=5_000_000.0,  # far above the field, yet nobody qualifies
            competitors=field,
            seed=SEED,
            iterations=DEFAULT_ITERATIONS,
            user_technical_pass_p=0.75,
        )
        assert result.win_probability == pytest.approx(0.75, abs=0.02)
        assert result.price_to_beat is None
        assert result.assumptions["price_to_beat_basis_iterations"] == 0

    def test_absent_competitors_report_none_not_zero_undercut(self) -> None:
        """A competitor who never bids has no undercut probability, not 0%."""
        field = [competitor(1), competitor(2, participation_p=0.0)]
        result = simulate(user_bid=1_000_000.0, competitors=field, seed=SEED, iterations=1000)
        assert result.undercut_probabilities[2] is None
        assert result.undercut_probabilities[1] is not None

    def test_extremely_high_bid_wins_almost_never(self) -> None:
        result = simulate(
            user_bid=100_000_000.0, competitors=FIELD, seed=SEED, iterations=DEFAULT_ITERATIONS
        )
        assert result.win_probability == 0.0
        assert result.rank_distribution.get(1, 0.0) == 0.0

    def test_extremely_low_bid_wins_whenever_qualified(self) -> None:
        result = simulate(
            user_bid=1.0,
            competitors=FIELD,
            seed=SEED,
            iterations=DEFAULT_ITERATIONS,
            user_technical_pass_p=0.95,
        )
        assert result.win_probability == pytest.approx(0.95, abs=0.02)

    def test_user_that_never_qualifies_never_wins(self) -> None:
        result = simulate(
            user_bid=1.0,
            competitors=FIELD,
            seed=SEED,
            iterations=1000,
            user_technical_pass_p=0.0,
        )
        assert result.win_probability == 0.0
        assert result.rank_distribution == {RANK_DISQUALIFIED: 1.0}

    def test_exact_tie_splits_evenly_between_two_bidders(self) -> None:
        """sigma == 0 makes the rival's bid exactly exp(log_mu); match it exactly."""
        rival = competitor(1, log_sigma=0.0)
        tie_price = math.exp(LOG_MU)
        result = simulate(
            user_bid=tie_price,
            competitors=[rival],
            seed=SEED,
            iterations=500,
            user_technical_pass_p=1.0,
        )
        assert result.win_probability == 0.5
        # Rank counts only strictly cheaper rivals, so tied bidders share rank 1.
        assert result.rank_distribution == {1: 1.0}
        # A tie is not an undercut.
        assert result.undercut_probabilities[1] == 0.0

    def test_exact_tie_splits_three_ways(self) -> None:
        rivals = [competitor(1, log_sigma=0.0), competitor(2, log_sigma=0.0)]
        result = simulate(
            user_bid=math.exp(LOG_MU),
            competitors=rivals,
            seed=SEED,
            iterations=500,
            user_technical_pass_p=1.0,
        )
        assert result.win_probability == pytest.approx(1.0 / 3.0)

    def test_a_hair_below_a_tie_wins_outright(self) -> None:
        rival = competitor(1, log_sigma=0.0)
        tie_price = math.exp(LOG_MU)
        result = simulate(
            user_bid=math.nextafter(tie_price, 0.0),
            competitors=[rival],
            seed=SEED,
            iterations=200,
            user_technical_pass_p=1.0,
        )
        assert result.win_probability == 1.0

    def test_a_hair_above_a_tie_loses_outright(self) -> None:
        rival = competitor(1, log_sigma=0.0)
        tie_price = math.exp(LOG_MU)
        result = simulate(
            user_bid=math.nextafter(tie_price, math.inf),
            competitors=[rival],
            seed=SEED,
            iterations=200,
            user_technical_pass_p=1.0,
        )
        assert result.win_probability == 0.0

    def test_single_iteration_is_legal(self) -> None:
        result = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=1)
        assert result.iterations == 1
        assert sum(result.rank_distribution.values()) == pytest.approx(1.0)


# --- price_to_beat on the output --------------------------------------------


class TestOutputPriceToBeat:
    def test_price_to_beat_sits_near_the_field_median_best_price(self) -> None:
        result = simulate(
            user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=DEFAULT_ITERATIONS
        )
        assert result.price_to_beat is not None
        # Best-of-three from a lognormal centred at 1e6 lands below the median.
        assert 700_000.0 < result.price_to_beat < 1_000_000.0

    def test_price_to_beat_is_independent_of_the_user_bid(self) -> None:
        low = simulate(user_bid=10.0, competitors=FIELD, seed=SEED, iterations=1000)
        high = simulate(user_bid=9_000_000.0, competitors=FIELD, seed=SEED, iterations=1000)
        assert low.price_to_beat == high.price_to_beat

    def test_beating_price_to_beat_gives_roughly_even_odds(self) -> None:
        base = simulate(
            user_bid=1_000_000.0,
            competitors=FIELD,
            seed=SEED,
            iterations=DEFAULT_ITERATIONS,
            user_technical_pass_p=1.0,
        )
        assert base.price_to_beat is not None
        at_the_price = simulate(
            user_bid=base.price_to_beat,
            competitors=FIELD,
            seed=SEED,
            iterations=DEFAULT_ITERATIONS,
            user_technical_pass_p=1.0,
        )
        assert at_the_price.win_probability == pytest.approx(0.5, abs=0.02)


# --- price_to_beat() from a curve -------------------------------------------


class TestPriceToBeatFunction:
    @staticmethod
    def linear_curve() -> list[CurvePoint]:
        """A hand-built curve: 1.0 at 100 falling linearly to 0.0 at 200."""
        return [CurvePoint(price, (200.0 - price) / 100.0, 0.0) for price in (100.0, 150.0, 200.0)]

    def test_interpolates_between_grid_points(self) -> None:
        assert price_to_beat(curve=self.linear_curve(), target_probability=0.75) == pytest.approx(
            125.0
        )

    def test_exact_grid_hit_returns_that_price(self) -> None:
        assert price_to_beat(curve=self.linear_curve(), target_probability=0.5) == pytest.approx(
            150.0
        )

    def test_unattainable_target_returns_none(self) -> None:
        """Even the cheapest simulated price misses the target: say so, don't guess."""
        curve = [CurvePoint(100.0, 0.4, 0.0), CurvePoint(200.0, 0.1, 0.0)]
        assert price_to_beat(curve=curve, target_probability=0.9) is None

    def test_target_cleared_everywhere_is_censored_at_the_top_of_the_grid(self) -> None:
        curve = [CurvePoint(100.0, 0.9, 0.0), CurvePoint(200.0, 0.85, 0.0)]
        assert price_to_beat(curve=curve, target_probability=0.5) == 200.0

    def test_unsorted_input_is_sorted(self) -> None:
        curve = list(reversed(self.linear_curve()))
        assert price_to_beat(curve=curve, target_probability=0.75) == pytest.approx(125.0)

    def test_accepts_tuples_and_mappings(self) -> None:
        as_tuples = [(100.0, 1.0), (150.0, 0.5), (200.0, 0.0)]
        as_dicts = [point.to_dict() for point in self.linear_curve()]
        assert price_to_beat(curve=as_tuples, target_probability=0.75) == pytest.approx(125.0)
        assert price_to_beat(curve=as_dicts, target_probability=0.75) == pytest.approx(125.0)

    def test_rejects_unusable_entries(self) -> None:
        with pytest.raises(ValueError, match="unsupported curve entry"):
            price_to_beat(curve=["nope"], target_probability=0.5)
        with pytest.raises(ValueError, match="price"):
            price_to_beat(curve=[{"win_probability": 0.5}], target_probability=0.5)

    def test_empty_curve_rejected(self) -> None:
        with pytest.raises(ValueError, match="curve must contain"):
            price_to_beat(curve=[], target_probability=0.5)

    def test_target_probability_validated(self) -> None:
        with pytest.raises(ValueError, match="target_probability"):
            price_to_beat(curve=self.linear_curve(), target_probability=1.2)

    def test_vertical_drop_returns_the_last_price_that_met_the_target(self) -> None:
        """A step curve has no interpolable bracket; the honest answer is the step."""
        curve = [CurvePoint(100.0, 0.6, 0.0), CurvePoint(101.0, 0.0, 0.0)]
        assert price_to_beat(curve=curve, target_probability=0.6) == 100.0

    def test_single_point_curve(self) -> None:
        assert price_to_beat(curve=[CurvePoint(100.0, 0.7, 0.0)], target_probability=0.6) == 100.0
        assert price_to_beat(curve=[CurvePoint(100.0, 0.5, 0.0)], target_probability=0.6) is None

    def test_round_trip_against_a_simulated_curve(self) -> None:
        grid = [700_000.0 + 20_000.0 * step for step in range(30)]
        curve = win_probability_curve(
            price_grid=grid,
            competitors=FIELD,
            seed=SEED,
            iterations=4000,
            user_technical_pass_p=1.0,
        )
        target = 0.6
        price = price_to_beat(curve=curve, target_probability=target)
        assert price is not None
        check = simulate(
            user_bid=price,
            competitors=FIELD,
            seed=SEED,
            iterations=4000,
            user_technical_pass_p=1.0,
        )
        # Interpolation between grid points, so allow the grid's own resolution.
        assert check.win_probability == pytest.approx(target, abs=0.05)

    def test_lower_targets_are_reachable_at_higher_prices(self) -> None:
        curve = self.linear_curve()
        cheap = price_to_beat(curve=curve, target_probability=0.9)
        dear = price_to_beat(curve=curve, target_probability=0.2)
        assert cheap is not None and dear is not None
        assert cheap < dear


# --- Undercut semantics ------------------------------------------------------


class TestUndercutProbabilities:
    def test_conditional_on_participation(self) -> None:
        """A rarely-seen competitor is still undercut half the time when they show."""
        rival = competitor(1, participation_p=0.10, log_sigma=0.0)
        result = simulate(
            user_bid=rival.median_bid * 0.5,
            competitors=[rival],
            seed=SEED,
            iterations=DEFAULT_ITERATIONS,
        )
        assert result.undercut_probabilities[1] == 1.0

    def test_high_bid_never_undercuts(self) -> None:
        rival = competitor(1, log_sigma=0.0)
        result = simulate(
            user_bid=rival.median_bid * 2.0,
            competitors=[rival],
            seed=SEED,
            iterations=500,
        )
        assert result.undercut_probabilities[1] == 0.0

    def test_undercut_is_monotone_in_price(self) -> None:
        previous = 1.1
        for price in (500_000.0, 800_000.0, 1_000_000.0, 1_400_000.0, 2_000_000.0):
            result = simulate(user_bid=price, competitors=FIELD, seed=SEED, iterations=2000)
            current = result.undercut_probabilities[1]
            assert current is not None
            assert current <= previous + 1e-12
            previous = current

    def test_every_competitor_appears_in_the_map(self) -> None:
        result = simulate(user_bid=1_000_000.0, competitors=FIELD, seed=SEED, iterations=500)
        assert set(result.undercut_probabilities) == {c.vendor_id for c in FIELD}


# --- Realism check against the corpus ---------------------------------------


class TestCorpusShapedScenario:
    """A field shaped like the real data: ~3 bidders, lowest qualified wins.

    Measured on the live database on 2026-09-10: 242 multi-bidder awarded tenders
    with offer values, winner was the cheapest offer 82.2% of the time, mean
    winner price-rank 1.256. This test does not assert that number — the engine
    cannot recover it without a calibrated technical-pass rate, which the corpus
    cannot supply (no offer row is ever technical_pass = false). It asserts only
    that a plausible field produces a sane, ordered rank distribution.
    """

    def test_rank_distribution_is_ordered_for_a_median_bid(self) -> None:
        field = [
            competitor(1, log_mu=LOG_MU, log_sigma=0.25, participation_p=0.9),
            competitor(2, log_mu=LOG_MU + 0.10, log_sigma=0.25, participation_p=0.7),
            competitor(3, log_mu=LOG_MU - 0.05, log_sigma=0.30, participation_p=0.5),
        ]
        result = simulate(
            user_bid=math.exp(LOG_MU),
            competitors=field,
            seed=SEED,
            iterations=DEFAULT_ITERATIONS,
            user_technical_pass_p=1.0,
        )
        assert result.convergence_ok is True
        assert result.rank_distribution[1] > result.rank_distribution.get(4, 0.0)
        assert sum(result.rank_distribution.values()) == pytest.approx(1.0)
        assert result.win_probability == pytest.approx(result.rank_distribution[1])

    def test_win_probability_equals_rank_one_mass_without_ties(self) -> None:
        """With continuous prices ties have probability zero, so the two agree."""
        result = simulate(
            user_bid=950_000.0,
            competitors=FIELD,
            seed=SEED,
            iterations=2000,
            user_technical_pass_p=0.9,
        )
        assert result.win_probability == pytest.approx(result.rank_distribution.get(1, 0.0))


def test_simulation_output_is_frozen() -> None:
    result = simulate(user_bid=1_000_000.0, competitors=[], seed=SEED, iterations=10)
    assert isinstance(result, SimulationOutput)
    with pytest.raises(AttributeError):
        result.win_probability = 0.5  # type: ignore[misc]
