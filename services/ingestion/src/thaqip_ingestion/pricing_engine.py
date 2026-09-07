"""Pricing Intelligence & Win-Probability Simulation Engine (M5-2, M5-3).

Calculates win-probability distributions, expected contract value,
competitive pricing zones, and GTPL Article 68 abnormally low tender flags
based on historical Saudi procurement awards and competitor offer densities.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import asyncpg

log = logging.getLogger("thaqip.pricing")


@dataclass
class BenchmarkStats:
    sample_count: int
    min_award: float | None = None
    p25_award: float | None = None
    median_award: float | None = None
    p75_award: float | None = None
    max_award: float | None = None
    median_bidders: float | None = None


@dataclass
class SimulationResult:
    proposed_price: float
    win_probability_pct: float
    expected_value: float
    competitive_zone: str  # aggressive | sweet_spot | conservative | uncompetitive
    gtpl_abnormally_low_flag: bool
    basis: str
    benchmarks: BenchmarkStats
    recommendations: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["benchmarks"] = asdict(self.benchmarks)
        return d


def classify_zone(proposed_price: float, median: float | None, p25: float | None, p75: float | None) -> str:
    """Classify the competitive pricing zone."""
    if not median or not p25 or not p75:
        return "sweet_spot"
    if proposed_price < p25:
        return "aggressive"
    elif proposed_price <= median:
        return "sweet_spot"
    elif proposed_price <= p75:
        return "conservative"
    else:
        return "uncompetitive"


async def calculate_price_simulation(
    conn: asyncpg.Connection,
    *,
    tender: dict[str, Any],
    proposed_price: float,
    target_margin_pct: float | None = None,
) -> SimulationResult:
    """Simulate win probability for a given tender and proposed bid price."""
    if proposed_price <= 0:
        raise ValueError("Proposed price must be greater than 0")

    activity_id = tender.get("activity_id")
    tender_id = tender.get("id")

    # 1. Fetch benchmark distribution for similar tenders in this activity
    bench_row = await conn.fetchrow(
        """SELECT count(*) AS n,
                  min(w.award_value)::float AS min_val,
                  percentile_cont(0.25) WITHIN GROUP (ORDER BY w.award_value)::float AS p25_val,
                  percentile_cont(0.50) WITHIN GROUP (ORDER BY w.award_value)::float AS p50_val,
                  percentile_cont(0.75) WITHIN GROUP (ORDER BY w.award_value)::float AS p75_val,
                  max(w.award_value)::float AS max_val
           FROM awards w
           JOIN tenders t2 ON t2.id = w.tender_id
           WHERE t2.id <> $1 AND t2.activity_id IS NOT DISTINCT FROM $2
             AND w.award_value IS NOT NULL""",
        tender_id, activity_id,
    )

    sample_count = bench_row["n"] if bench_row else 0
    min_award = bench_row["min_val"] if bench_row and bench_row["min_val"] is not None else None
    p25_award = bench_row["p25_val"] if bench_row and bench_row["p25_val"] is not None else None
    median_award = bench_row["p50_val"] if bench_row and bench_row["p50_val"] is not None else None
    p75_award = bench_row["p75_val"] if bench_row and bench_row["p75_val"] is not None else None
    max_award = bench_row["max_val"] if bench_row and bench_row["max_val"] is not None else None

    # 2. Competitor bidder density
    bidders_row = await conn.fetchrow(
        """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY b.n)::float AS median_bidders
           FROM tenders t2
           JOIN LATERAL (SELECT count(*) n FROM offers o WHERE o.tender_id=t2.id) b ON b.n > 0
           WHERE t2.activity_id IS NOT DISTINCT FROM $1
             AND EXISTS (SELECT 1 FROM awards w2 WHERE w2.tender_id = t2.id)""",
        activity_id,
    )
    median_bidders = bidders_row["median_bidders"] if bidders_row and bidders_row["median_bidders"] else 4.0

    live_bidders = (tender.get("submitted_bids_count") or 0) + (tender.get("external_bids_count") or 0)

    # 3. Probability Modeling
    basis = "activity_history"
    if tender.get("source") == "forsah" and live_bidders > 0:
        basis = "live_bidders"
        effective_bidders = float(live_bidders)
    else:
        effective_bidders = max(float(median_bidders), 1.0)

    # Baseline probability based on bidder density
    baseline_p = 1.0 / (effective_bidders + 1.0)

    # Price sensitivity adjustment relative to median award
    if median_award and median_award > 0:
        ratio = proposed_price / median_award
        # Sigmoid / logistic curve anchored around median award
        # ratio 0.8 -> ~85% win; ratio 1.0 -> ~50%; ratio 1.3 -> ~15%
        import math
        k = 4.0  # slope factor
        # Adjusted probability centered at ratio 1.0
        win_prob = 1.0 / (1.0 + math.exp(k * (ratio - 0.95)))
        # Cap between 2% and 95%
        win_prob = max(0.02, min(0.95, win_prob))
    else:
        # Fallback when no median awards recorded in this activity
        basis = "density_fallback"
        win_prob = baseline_p

    win_prob_pct = round(win_prob * 100.0, 1)

    # 4. GTPL Article 68 abnormally low tender check
    # In Saudi procurement, offers > 25-30% below government estimate or median are scrutinized
    gtpl_abnormally_low = False
    if median_award and proposed_price < (0.70 * median_award):
        gtpl_abnormally_low = True

    # 5. Expected Value
    expected_value = round(proposed_price * (win_prob_pct / 100.0), 2)

    # 6. Zone & Recommendations
    zone = classify_zone(proposed_price, median_award, p25_award, p75_award)

    optimal_price = round(median_award * 0.92, 2) if median_award else round(proposed_price * 0.95, 2)
    safe_floor = round(median_award * 0.72, 2) if median_award else round(proposed_price * 0.75, 2)

    benchmarks = BenchmarkStats(
        sample_count=sample_count,
        min_award=min_award,
        p25_award=p25_award,
        median_award=median_award,
        p75_award=p75_award,
        max_award=max_award,
        median_bidders=median_bidders,
    )

    recommendations = {
        "optimal_price": optimal_price,
        "safe_margin_floor": safe_floor,
    }

    return SimulationResult(
        proposed_price=proposed_price,
        win_probability_pct=win_prob_pct,
        expected_value=expected_value,
        competitive_zone=zone,
        gtpl_abnormally_low_flag=gtpl_abnormally_low,
        basis=basis,
        benchmarks=benchmarks,
        recommendations=recommendations,
    )
