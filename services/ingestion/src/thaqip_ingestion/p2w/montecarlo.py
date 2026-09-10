"""Monte Carlo win-probability engine for Price-to-Win (PRD FR-009).

What this module does
---------------------
Given a candidate bid from the operator and a set of *competitor draws* — each a
participation probability, a lognormal price distribution and a technical-pass
probability — it simulates the tender many times and reports how often the
operator's bid would have won, where it would have ranked, and how far the field
would have to be beaten.

The evaluation rule modelled here is ``lowest_qualified``: among the bidders that
actually showed up *and* passed the technical evaluation, the lowest price wins.
That is not a modelling convenience, it is roughly what the corpus says. Measured
against the live database on 2026-09-10, across the 242 multi-bidder awarded
tenders that carry offer values, the winner was the lowest-priced offer 82.2% of
the time, with a mean winner price-rank of 1.256. Saudi public tenders behave
close to a lowest-qualified-price auction, but the residual ~18% is real and this
model does not explain it — see the limitations below.

A caveat that matters for calibration: in this database ``offers.technical_pass``
is ``true`` (1038 rows) or NULL (109 rows) and *never* false. There is not one
observed technical failure in the corpus, so ``technical_pass_p`` — for the user
and for every competitor — is an assumption supplied by the caller, not a
quantity estimated from data. The 17.8% of awards that did not go to the cheapest
offer are the only signal that something other than price decides, and this model
absorbs all of it into the technical-pass Bernoullis.

Tie handling
------------
An exact price tie between the user and one or more qualified competitors is
resolved by *splitting the win evenly*: with ``k`` bidders tied at the winning
price the user receives ``1/k`` of a win in that iteration. This is the neutral
assumption — the real tie-break (earliest submission, technical score, a
re-quote) is not observable in the data, so the engine refuses to invent an
advantage in either direction. Rank is unaffected by ties: a bidder's rank counts
only the qualified competitors *strictly* below their price, so tied bidders
share a rank.

Determinism
-----------
Hard requirement (house rule 7). Every random number comes from a
``random.Random(seed)`` instance created inside this module; the global ``random``
module is never touched and nothing is seeded from the clock. Identical
``(inputs, model_version, seed)`` reproduce an identical ``SimulationOutput``,
field for field. ``SimulationOutput`` carries no timestamp for exactly this
reason.

The guarantee is *within a Python runtime*. Mersenne Twister is stable across
CPython versions, but ``Random.gauss`` is a library implementation detail (it
caches a spare Box-Muller value, so the number of underlying ``random()`` calls
alternates); a future CPython could change it. Stored predictions therefore pin
``model_version`` alongside the seed, and a replay that must match bit for bit
should be run on the same interpreter that produced it.

Common random numbers
---------------------
``win_probability_curve`` draws the competitor scenarios **once** and re-uses the
same draws at every price on the grid. This is what makes the curve monotone
non-increasing and smooth: with independent draws per grid point the curve would
wiggle from sampling noise alone, which reads to a user as "bidding lower
sometimes hurts" — a product bug, not a finding. Under ``lowest_qualified`` with
fixed scenarios the per-iteration win credit is a non-increasing step function of
price, so the average across iterations is monotone by construction.

Honest limitations
------------------
* Garbage in, garbage out. This module is a *sampler*, not an estimator: it does
  not know whether ``log_mu``/``log_sigma``/``participation_p`` were fitted on 40
  observations or on 2. Whether a competitor may be modelled at all is an
  evidence-tier decision that belongs upstream (``p2w.evidence`` /
  ``p2w.competitor``); a suppressed competitor should simply not be passed in.
  Nothing here should be shown to a user as a competitor price claim.
* Bids are assumed **independent across competitors and across iterations**. Real
  tenders have common shocks (a BOQ everyone prices off, steel prices, a
  clarification round) that induce positive correlation. Correlated bidders make
  the true win probability more extreme than this model reports near the middle
  of the distribution.
* Participation and technical qualification are independent Bernoullis,
  independent of the price drawn. In reality an underpriced bid is *more* likely
  to fail technical evaluation; the model does not capture that link.
* The number of bidders is exactly the number of competitors supplied. Unknown
  entrants — a vendor that never bid this agency before — are not simulated
  unless the caller adds them as an explicit ``CompetitorDraw``.
* Sampling error is real and reported: ``standard_error`` and ``convergence_ok``
  are part of the output, and a result with ``convergence_ok is False`` should be
  displayed with its uncertainty, not as a point number.
* The rule itself is an approximation. 17.8% of awarded multi-bidder tenders in
  this corpus did not go to the cheapest offer, and the model has no mechanism
  for that other than a technical-pass draw the data cannot calibrate. Treat a
  ``lowest_qualified`` win probability as an upper bound on the informativeness
  of price alone.
* ``price_to_beat`` on the output is a *median* of the field's best qualified
  price, not a guarantee; beating it wins against the field in about half of the
  simulated worlds and says nothing about the user's own technical risk.
"""
from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from random import Random
from typing import Any

from .contracts import MODEL_VERSION

# --- Named constants ---------------------------------------------------------
# Every magic number in this module lives here with the reason it has that value.

#: Default iteration count for a single `simulate` call. Chosen so that the worst
#: case standard error, sqrt(0.25/n), is 0.0079 — comfortably inside
#: MAX_STANDARD_ERROR at every possible win probability.
DEFAULT_ITERATIONS = 4000

#: Default iterations per grid point for `win_probability_curve`. Lower than
#: DEFAULT_ITERATIONS because a curve pays the cost at every price; common random
#: numbers keep the *shape* exact (monotone) even when each level is noisier.
#: sqrt(0.25/2000) = 0.0112, so mid-curve points may legitimately report
#: convergence_ok False.
DEFAULT_CURVE_ITERATIONS = 2000

#: A simulated probability is treated as converged when its binomial standard
#: error is at or below one percentage point. One point is the granularity the
#: UI shows ("62%"), so a tighter bar would be false precision and a looser one
#: would let the displayed digit move between runs.
MAX_STANDARD_ERROR = 0.01

#: Default probability that the operator's own bid passes technical evaluation.
#: 0.95 is a deliberately optimistic-but-not-certain prior for a bidder who
#: prepared the submission; callers with real compliance data should override it.
DEFAULT_USER_TECHNICAL_PASS_P = 0.95

#: The only evaluation rule backed by evidence in this corpus.
EVALUATION_RULE_LOWEST_QUALIFIED = "lowest_qualified"
EVALUATION_RULES: tuple[str, ...] = (EVALUATION_RULE_LOWEST_QUALIFIED,)

#: Rank bucket used when the operator's own bid failed technical evaluation in an
#: iteration. There is no meaningful price rank in that world, and folding it
#: into rank 1 or into the tail would both be lies, so it gets its own bucket.
RANK_DISQUALIFIED = 0

#: Upper bound on iterations, to stop a caller (or a bad UI slider) turning a
#: request into a multi-minute CPU burn. 200k iterations is ~50x the default and
#: buys a standard error of 0.0011, far past the point of useful precision.
MAX_ITERATIONS = 200_000

#: Tie-break policy identifier reported in `assumptions`, so a consumer can tell
#: which convention produced a 0.5 and does not have to guess.
TIE_POLICY = "split_evenly"

#: How `undercut_probabilities` is defined, reported in `assumptions`.
UNDERCUT_DEFINITION = "P(user_bid < competitor_bid | competitor participates)"

#: Largest exponent `math.exp` can represent - beyond it a sampled price
#: overflows. A CompetitorDraw whose price distribution puts representable mass
#: past this point is rejected at construction, so callers get this module's
#: documented ValueError at the boundary instead of an OverflowError raised from
#: inside the sampler after the run has already started.
MAX_LOG_PRICE = math.log(sys.float_info.max)

#: How far into the tail construction must stay representable. A |z| beyond 8 has
#: probability ~1e-15 per draw, so at MAX_ITERATIONS the residual chance of an
#: overflow surviving this check is ~1e-10 - and the sampler degrades even that
#: case to an infinite (never-winning) bid rather than raising.
LOG_PRICE_TAIL_SIGMAS = 8.0


# --- Inputs ------------------------------------------------------------------


@dataclass(frozen=True)
class CompetitorDraw:
    """One competitor's simulated behaviour: will they bid, at what price, and
    will they qualify.

    ``log_mu`` / ``log_sigma`` are the parameters of the *log* of the price: a
    sampled bid is ``exp(log_mu + log_sigma * z)`` with ``z`` standard normal, so
    ``exp(log_mu)`` is the median bid. Lognormal because prices are strictly
    positive and right-skewed, and because a multiplicative error ("15% above the
    reference") is the way bidders actually think.

    ``log_sigma == 0`` is legal and yields a deterministic bid of ``exp(log_mu)``
    — useful for a known published price and for exercising exact ties.
    """

    vendor_id: int
    participation_p: float
    log_mu: float
    log_sigma: float
    technical_pass_p: float

    def __post_init__(self) -> None:
        _require_probability("participation_p", self.participation_p)
        _require_probability("technical_pass_p", self.technical_pass_p)
        _require_finite("log_mu", self.log_mu)
        sigma = _require_finite("log_sigma", self.log_sigma)
        if sigma < 0.0:
            raise ValueError(f"log_sigma must be >= 0, got {sigma}")
        # exp() overflows past MAX_LOG_PRICE. Catching it here keeps the promise
        # made in `simulate`: invalid input raises ValueError, naming the field.
        tail = self.log_mu + LOG_PRICE_TAIL_SIGMAS * sigma
        if tail > MAX_LOG_PRICE:
            raise ValueError(
                f"log_mu + {LOG_PRICE_TAIL_SIGMAS} * log_sigma must be "
                f"<= {MAX_LOG_PRICE} for the price to be representable, got {tail}"
            )

    @property
    def median_bid(self) -> float:
        """The median of the modelled price distribution, ``exp(log_mu)``."""
        return math.exp(self.log_mu)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id,
            "participation_p": self.participation_p,
            "log_mu": self.log_mu,
            "log_sigma": self.log_sigma,
            "technical_pass_p": self.technical_pass_p,
        }


# --- Outputs -----------------------------------------------------------------


@dataclass(frozen=True)
class SimulationOutput:
    """Result of one simulation at one candidate bid.

    ``rank_distribution`` maps rank -> probability and sums to 1.0 (up to float
    error), with ``RANK_DISQUALIFIED`` (0) holding the mass where the operator's
    own bid failed technical evaluation.

    ``undercut_probabilities`` maps vendor_id -> probability that the operator's
    bid is strictly below that competitor's price *given that the competitor
    participates*, or ``None`` for a competitor that never participated in any
    iteration. ``None`` rather than 0.0 on purpose: "no evidence" and "never
    undercuts" are different statements and the UI must not conflate them.
    """

    win_probability: float
    rank_distribution: dict[int, float]
    undercut_probabilities: dict[int, float | None]
    price_to_beat: float | None
    iterations: int
    seed: int
    standard_error: float
    convergence_ok: bool
    assumptions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "win_probability": self.win_probability,
            "rank_distribution": {str(k): v for k, v in self.rank_distribution.items()},
            "undercut_probabilities": {
                str(k): v for k, v in self.undercut_probabilities.items()
            },
            "price_to_beat": self.price_to_beat,
            "iterations": self.iterations,
            "seed": self.seed,
            "standard_error": self.standard_error,
            "convergence_ok": self.convergence_ok,
            "assumptions": dict(self.assumptions),
        }


@dataclass(frozen=True)
class CurvePoint:
    """One point of a win-probability curve: price -> simulated win probability."""

    price: float
    win_probability: float
    standard_error: float

    def to_dict(self) -> dict[str, float]:
        return {
            "price": self.price,
            "win_probability": self.win_probability,
            "standard_error": self.standard_error,
        }


# --- Validation helpers ------------------------------------------------------


def _require_finite(name: str, value: Any) -> float:
    """Coerce to a finite float or raise ValueError naming the field.

    ValueError for both bad values and bad types, matching `p2w.contracts`.
    """
    if value is None:
        raise ValueError(f"{name} must not be None")
    if isinstance(value, bool):
        # TRY004 is suppressed throughout this module on purpose: `p2w.contracts`
        # raises ValueError for both bad values and bad types, and one exception
        # type across the whole P2W validation surface is worth more to callers
        # than the TypeError/ValueError split.
        raise ValueError(f"{name} must be numeric, got bool")  # noqa: TRY004
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric, got {type(value).__name__}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {number}")
    return number


def _require_probability(name: str, value: Any) -> float:
    number = _require_finite(name, value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be in [0,1], got {number}")
    return number


def _validate_competitors(competitors: Iterable[CompetitorDraw]) -> tuple[CompetitorDraw, ...]:
    """Freeze the input order and reject duplicate vendor ids.

    Order matters because it fixes the order random numbers are consumed in, and
    therefore the exact output for a given seed. Duplicate vendor ids are
    rejected because the undercut map is keyed by vendor id and would silently
    lose one of them.
    """
    frozen = tuple(competitors)
    for index, competitor in enumerate(frozen):
        if not isinstance(competitor, CompetitorDraw):
            raise ValueError(  # noqa: TRY004
                f"competitors[{index}] must be a CompetitorDraw, "
                f"got {type(competitor).__name__}"
            )
    seen: set[int] = set()
    for competitor in frozen:
        if competitor.vendor_id in seen:
            raise ValueError(f"duplicate vendor_id in competitors: {competitor.vendor_id}")
        seen.add(competitor.vendor_id)
    return frozen


def _validate_iterations(iterations: Any) -> int:
    if isinstance(iterations, bool) or not isinstance(iterations, int):
        raise ValueError(  # noqa: TRY004
            f"iterations must be an int, got {type(iterations).__name__}"
        )
    if iterations < 1:
        raise ValueError(f"iterations must be >= 1, got {iterations}")
    if iterations > MAX_ITERATIONS:
        raise ValueError(f"iterations must be <= {MAX_ITERATIONS}, got {iterations}")
    return iterations


def _validate_seed(seed: Any) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed must be an int, got {type(seed).__name__}")  # noqa: TRY004
    return seed


def _validate_rule(evaluation_rule: str) -> str:
    if evaluation_rule not in EVALUATION_RULES:
        raise ValueError(
            f"evaluation_rule must be one of {EVALUATION_RULES}, got {evaluation_rule!r}"
        )
    return evaluation_rule


def _validate_bid(name: str, value: Any) -> float:
    number = _require_finite(name, value)
    if number <= 0.0:
        raise ValueError(f"{name} must be > 0, got {number}")
    return number


# --- Scenario sampling -------------------------------------------------------

#: One competitor's realised behaviour in one iteration:
#: (vendor_id, bid, participated, technically_qualified).
_Participant = tuple[int, float, bool, bool]
#: One iteration: the competitors' realisations plus whether the user qualified.
_Scenario = tuple[tuple[_Participant, ...], bool]


def _sampled_price(exponent: float) -> float:
    """``exp(exponent)``, degrading a beyond-8-sigma overflow to ``inf``.

    Construction already rejects a draw whose 8-sigma tail is unrepresentable, so
    reaching this branch takes a ~1e-15 draw. An infinite price is the truthful
    outcome there - a bid that large loses to everything - and is far better than
    an OverflowError that kills a simulation mid-flight.
    """
    try:
        return math.exp(exponent)
    except OverflowError:  # pragma: no cover - needs a |z| beyond 8
        return math.inf


def _draw_scenarios(
    competitors: Sequence[CompetitorDraw],
    *,
    seed: int,
    iterations: int,
    user_technical_pass_p: float,
) -> list[_Scenario]:
    """Draw every iteration's competitor behaviour once, independently of price.

    Nothing here depends on the user's bid, which is precisely what allows the
    same scenarios to be re-used across a whole price grid (common random
    numbers). The draw order — for each competitor: participation, price,
    technical pass; then the user's technical pass — is fixed and part of the
    determinism contract.
    """
    rng = Random(seed)
    scenarios: list[_Scenario] = []
    for _ in range(iterations):
        participants: list[_Participant] = []
        for competitor in competitors:
            participates = rng.random() < competitor.participation_p
            z = rng.gauss(0.0, 1.0)
            qualifies = rng.random() < competitor.technical_pass_p
            bid = _sampled_price(competitor.log_mu + competitor.log_sigma * z)
            participants.append((competitor.vendor_id, bid, participates, qualifies))
        user_qualified = rng.random() < user_technical_pass_p
        scenarios.append((tuple(participants), user_qualified))
    return scenarios


def _win_credit(scenario: _Scenario, user_bid: float) -> float:
    """Credit the user earns in one iteration under ``lowest_qualified``.

    1.0 for a clean win, ``1/k`` when tied with ``k-1`` qualified competitors at
    the same price, 0.0 otherwise. Monotone non-increasing in ``user_bid`` for a
    fixed scenario — the property the curve depends on.
    """
    participants, user_qualified = scenario
    if not user_qualified:
        return 0.0
    ties = 1  # the user
    for _vendor_id, bid, participates, qualifies in participants:
        if not (participates and qualifies):
            continue
        if bid < user_bid:
            return 0.0
        if bid == user_bid:
            ties += 1
    return 1.0 / ties


def _user_rank(scenario: _Scenario, user_bid: float) -> int:
    """The user's price rank among qualified bidders, or RANK_DISQUALIFIED.

    Rank counts only qualified competitors *strictly* cheaper than the user, so
    tied bidders share a rank (two bidders tied at the lowest price are both
    rank 1) rather than one being arbitrarily promoted.
    """
    participants, user_qualified = scenario
    if not user_qualified:
        return RANK_DISQUALIFIED
    cheaper = 0
    for _vendor_id, bid, participates, qualifies in participants:
        if participates and qualifies and bid < user_bid:
            cheaper += 1
    return cheaper + 1


def _best_qualified_bid(scenario: _Scenario) -> float | None:
    """Lowest price among qualified participating competitors, or None."""
    best: float | None = None
    for _vendor_id, bid, participates, qualifies in scenario[0]:
        if participates and qualifies and (best is None or bid < best):
            best = bid
    return best


def _median(values: Sequence[float]) -> float:
    """Median of a non-empty sequence. Pure python: numpy is not installed."""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def standard_error_of_proportion(probability: float, iterations: int) -> float:
    """Binomial standard error ``sqrt(p*(1-p)/n)`` of a simulated proportion.

    Reported rather than hidden: it is the honest statement of how much of a
    displayed win probability is sampling noise.
    """
    p = _require_probability("probability", probability)
    n = _validate_iterations(iterations)
    return math.sqrt(p * (1.0 - p) / n)


# --- Public API --------------------------------------------------------------


def simulate(
    *,
    user_bid: float,
    competitors: Sequence[CompetitorDraw],
    seed: int,
    iterations: int = DEFAULT_ITERATIONS,
    user_technical_pass_p: float = DEFAULT_USER_TECHNICAL_PASS_P,
    evaluation_rule: str = EVALUATION_RULE_LOWEST_QUALIFIED,
) -> SimulationOutput:
    """Simulate the tender ``iterations`` times and report the user's outcome.

    Each iteration: sample every competitor's participation (Bernoulli), price
    (lognormal) and technical qualification (Bernoulli); sample the user's own
    technical qualification; then apply ``evaluation_rule``. Under
    ``lowest_qualified`` the user wins when technically qualified and strictly
    cheapest, and splits the win evenly on an exact tie (see the module
    docstring).

    With no competitors the answer is analytic — the user wins exactly when they
    qualify — so the function returns ``win_probability == user_technical_pass_p``
    exactly rather than a sampled approximation of it.

    Raises ValueError on any invalid input; never returns a silently degraded
    result.
    """
    user_bid = _validate_bid("user_bid", user_bid)
    frozen = _validate_competitors(competitors)
    seed = _validate_seed(seed)
    iterations = _validate_iterations(iterations)
    user_technical_pass_p = _require_probability(
        "user_technical_pass_p", user_technical_pass_p
    )
    evaluation_rule = _validate_rule(evaluation_rule)

    assumptions: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "evaluation_rule": evaluation_rule,
        "tie_policy": TIE_POLICY,
        "undercut_definition": UNDERCUT_DEFINITION,
        "price_distribution": "lognormal",
        "independent_competitors": True,
        "user_technical_pass_p": user_technical_pass_p,
        "competitor_count": len(frozen),
        "user_bid": user_bid,
        "analytic": not frozen,
    }

    if not frozen:
        # No field to lose to: the user wins iff technically qualified. Sampling
        # this would only add noise to a number we know exactly.
        win_probability = user_technical_pass_p
        se = standard_error_of_proportion(win_probability, iterations)
        return SimulationOutput(
            win_probability=win_probability,
            rank_distribution={
                RANK_DISQUALIFIED: 1.0 - user_technical_pass_p,
                1: user_technical_pass_p,
            },
            undercut_probabilities={},
            price_to_beat=None,
            iterations=iterations,
            seed=seed,
            standard_error=se,
            convergence_ok=se <= MAX_STANDARD_ERROR,
            assumptions=assumptions,
        )

    scenarios = _draw_scenarios(
        frozen,
        seed=seed,
        iterations=iterations,
        user_technical_pass_p=user_technical_pass_p,
    )

    win_credit = 0.0
    rank_counts: dict[int, float] = {}
    participation_counts: dict[int, int] = {c.vendor_id: 0 for c in frozen}
    undercut_counts: dict[int, int] = {c.vendor_id: 0 for c in frozen}
    best_bids: list[float] = []

    for scenario in scenarios:
        win_credit += _win_credit(scenario, user_bid)
        rank = _user_rank(scenario, user_bid)
        rank_counts[rank] = rank_counts.get(rank, 0.0) + 1.0
        for vendor_id, bid, participates, _qualifies in scenario[0]:
            if not participates:
                continue
            participation_counts[vendor_id] += 1
            if user_bid < bid:
                undercut_counts[vendor_id] += 1
        best = _best_qualified_bid(scenario)
        if best is not None:
            best_bids.append(best)

    win_probability = win_credit / iterations
    # Float arithmetic on averaged credits can land a hair outside [0,1].
    win_probability = min(1.0, max(0.0, win_probability))
    se = standard_error_of_proportion(win_probability, iterations)

    rank_distribution = {
        rank: count / iterations for rank, count in sorted(rank_counts.items())
    }
    undercut_probabilities: dict[int, float | None] = {}
    for vendor_id, participated in participation_counts.items():
        undercut_probabilities[vendor_id] = (
            undercut_counts[vendor_id] / participated if participated else None
        )

    assumptions["price_to_beat_basis_iterations"] = len(best_bids)
    assumptions["price_to_beat_definition"] = (
        "median lowest qualified competitor price across simulated iterations"
    )

    return SimulationOutput(
        win_probability=win_probability,
        rank_distribution=rank_distribution,
        undercut_probabilities=undercut_probabilities,
        price_to_beat=_median(best_bids) if best_bids else None,
        iterations=iterations,
        seed=seed,
        standard_error=se,
        convergence_ok=se <= MAX_STANDARD_ERROR,
        assumptions=assumptions,
    )


def win_probability_curve(
    *,
    price_grid: Sequence[float],
    competitors: Sequence[CompetitorDraw],
    seed: int,
    iterations: int = DEFAULT_CURVE_ITERATIONS,
    user_technical_pass_p: float = DEFAULT_USER_TECHNICAL_PASS_P,
    evaluation_rule: str = EVALUATION_RULE_LOWEST_QUALIFIED,
) -> list[CurvePoint]:
    """Win probability at each price on ``price_grid``, using common random numbers.

    The competitor scenarios are drawn once and evaluated at every price, so the
    returned curve is monotone non-increasing in price *exactly*, not just on
    average. Points are returned sorted by ascending price; duplicate prices in
    the grid are collapsed.

    Each point's ``win_probability`` equals what ``simulate`` returns for that
    price with the same ``seed`` and ``iterations`` — the two entry points share
    one sampler.
    """
    frozen = _validate_competitors(competitors)
    seed = _validate_seed(seed)
    iterations = _validate_iterations(iterations)
    user_technical_pass_p = _require_probability(
        "user_technical_pass_p", user_technical_pass_p
    )
    _validate_rule(evaluation_rule)

    prices = sorted({_validate_bid("price_grid entry", price) for price in price_grid})
    if not prices:
        raise ValueError("price_grid must contain at least one price")

    if not frozen:
        # Flat by construction: with no field, price does not affect the outcome.
        se = standard_error_of_proportion(user_technical_pass_p, iterations)
        return [CurvePoint(price, user_technical_pass_p, se) for price in prices]

    scenarios = _draw_scenarios(
        frozen,
        seed=seed,
        iterations=iterations,
        user_technical_pass_p=user_technical_pass_p,
    )

    points: list[CurvePoint] = []
    for price in prices:
        credit = 0.0
        for scenario in scenarios:
            credit += _win_credit(scenario, price)
        probability = min(1.0, max(0.0, credit / iterations))
        points.append(
            CurvePoint(price, probability, standard_error_of_proportion(probability, iterations))
        )
    return points


def _curve_pairs(curve: Iterable[Any]) -> list[tuple[float, float]]:
    """Normalise a curve into sorted (price, win_probability) pairs.

    Accepts CurvePoint objects, ``(price, probability)`` pairs, or mappings with
    ``price``/``win_probability`` keys, so a curve that has been through JSON is
    still usable.
    """
    pairs: list[tuple[float, float]] = []
    for item in curve:
        if isinstance(item, CurvePoint):
            price, probability = item.price, item.win_probability
        elif isinstance(item, Mapping):
            try:
                price, probability = item["price"], item["win_probability"]
            except KeyError as exc:
                raise ValueError(
                    "curve mapping entries need 'price' and 'win_probability' keys"
                ) from exc
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            price, probability = item
        else:
            raise ValueError(f"unsupported curve entry: {type(item).__name__}")
        pairs.append(
            (_validate_bid("price", price), _require_probability("win_probability", probability))
        )
    if not pairs:
        raise ValueError("curve must contain at least one point")
    pairs.sort(key=lambda pair: pair[0])
    return pairs


def price_to_beat(*, curve: Iterable[Any], target_probability: float) -> float | None:
    """Highest price whose simulated win probability still reaches the target.

    Linear interpolation between the two grid points that bracket the target.
    Returns ``None`` when the target is unattainable on this grid — i.e. even the
    cheapest price simulated wins less often than ``target_probability``; the
    honest answer there is "not at any price we simulated", never an extrapolated
    price below the grid.

    When *every* point on the grid clears the target the answer is censored by
    the grid: the highest grid price is returned, because the true crossing lies
    somewhere above it and inventing that value would be extrapolation.
    """
    target = _require_probability("target_probability", target_probability)
    pairs = _curve_pairs(curve)

    if pairs[0][1] < target:
        return None
    if pairs[-1][1] >= target:
        return pairs[-1][0]

    for index in range(1, len(pairs)):
        prev_price, prev_probability = pairs[index - 1]
        price, probability = pairs[index]
        if probability >= target:
            continue
        if prev_probability <= target:
            # Degenerate bracket (a vertical drop, or a non-monotone input):
            # the last price that met the target is the only defensible answer.
            return prev_price
        span = prev_probability - probability
        weight = (prev_probability - target) / span
        return prev_price + weight * (price - prev_price)
    return pairs[-1][0]  # pragma: no cover - unreachable given the guards above


__all__ = [
    "DEFAULT_CURVE_ITERATIONS",
    "DEFAULT_ITERATIONS",
    "DEFAULT_USER_TECHNICAL_PASS_P",
    "EVALUATION_RULES",
    "EVALUATION_RULE_LOWEST_QUALIFIED",
    "LOG_PRICE_TAIL_SIGMAS",
    "MAX_ITERATIONS",
    "MAX_LOG_PRICE",
    "MAX_STANDARD_ERROR",
    "RANK_DISQUALIFIED",
    "TIE_POLICY",
    "UNDERCUT_DEFINITION",
    "CompetitorDraw",
    "CurvePoint",
    "SimulationOutput",
    "price_to_beat",
    "simulate",
    "standard_error_of_proportion",
    "win_probability_curve",
]
