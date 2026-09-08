"""Seed baseline pricing simulations for the accuracy calibration loop.

The console exposes the same idea as an operator button. This module exists so
Modal/cron can run the seeding loop without depending on the console service.
It creates at most one seeded simulation for each active pursuit that does not
already have a simulation. Later, when awards are harvested, the accuracy loop
can measure these hypotheses against actual award values.

Run:
  DATABASE_URL=... python -m thaqip_ingestion.pricing_seed --limit 100
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any

import asyncpg

from . import db

log = logging.getLogger("thaqip.pricing_seed")


@dataclass(frozen=True)
class SeededSimulation:
    simulation_id: int
    pursuit_id: int
    proposed_price: float
    win_probability_pct: float
    basis: str
    sample_count: int


def _baseline_price(*, median_award: float | None, booklet_price: float | None) -> tuple[float, str]:
    if median_award:
        return round(median_award * 0.92, 2), "baseline_activity_history"
    if booklet_price and booklet_price > 0:
        return round(max(booklet_price * 120.0, 10_000.0), 2), "baseline_booklet_price"
    return 100_000.0, "baseline_default"


def _win_probability_pct(*, proposed_price: float, median_award: float | None, live_bidders: int) -> float:
    effective_bidders = max(float(live_bidders or 4), 1.0)
    if median_award and median_award > 0:
        ratio = proposed_price / median_award
        win_prob = max(0.02, min(0.95, 1.0 / (1.0 + math.exp(4.0 * (ratio - 0.95)))))
    else:
        win_prob = 1.0 / (effective_bidders + 1.0)
    return round(win_prob * 100.0, 1)


async def seed_pricing_baselines(pool: asyncpg.Pool, *, limit: int = 100) -> list[SeededSimulation]:
    rows = await pool.fetch(
        """SELECT p.id AS pursuit_id, t.id AS tender_id, t.activity_id,
                  t.booklet_price::float AS booklet_price,
                  t.submitted_bids_count, t.external_bids_count
           FROM pursuits p
           JOIN tenders t ON t.id = p.tender_id
           WHERE p.stage NOT IN ('submitted', 'won', 'lost')
             AND NOT EXISTS (
               SELECT 1 FROM pursuit_simulations s WHERE s.pursuit_id = p.id
             )
           ORDER BY p.updated_at DESC, p.id DESC
           LIMIT $1""",
        limit,
    )
    seeded: list[SeededSimulation] = []
    async with pool.acquire() as conn:
        for r in rows:
            bench = await conn.fetchrow(
                """SELECT count(*)::int AS n,
                          percentile_cont(0.25) WITHIN GROUP (ORDER BY w.award_value)::float AS p25_award,
                          percentile_cont(0.50) WITHIN GROUP (ORDER BY w.award_value)::float AS median_award,
                          percentile_cont(0.75) WITHIN GROUP (ORDER BY w.award_value)::float AS p75_award
                   FROM awards w
                   JOIN tenders t2 ON t2.id = w.tender_id
                   WHERE t2.id <> $1 AND t2.activity_id IS NOT DISTINCT FROM $2
                     AND w.award_value IS NOT NULL""",
                r["tender_id"], r["activity_id"],
            )
            median_award = bench["median_award"] if bench else None
            proposed_price, basis = _baseline_price(
                median_award=median_award,
                booklet_price=r["booklet_price"],
            )
            live_bidders = (r["submitted_bids_count"] or 0) + (r["external_bids_count"] or 0)
            win_prob_pct = _win_probability_pct(
                proposed_price=proposed_price,
                median_award=median_award,
                live_bidders=live_bidders,
            )
            expected_value = round(proposed_price * (win_prob_pct / 100.0), 2)
            metadata: dict[str, Any] = {
                "seeded": True,
                "sample_count": bench["n"] if bench else 0,
                "p25_award": bench["p25_award"] if bench else None,
                "median_award": median_award,
                "p75_award": bench["p75_award"] if bench else None,
                "live_bidders": live_bidders,
            }
            sim_id = await conn.fetchval(
                """INSERT INTO pursuit_simulations
                     (pursuit_id, proposed_price, win_pct, expected_value, basis, metadata)
                   VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                   RETURNING id""",
                r["pursuit_id"], proposed_price, win_prob_pct, expected_value, basis, json.dumps(metadata),
            )
            seeded.append(SeededSimulation(
                simulation_id=sim_id,
                pursuit_id=r["pursuit_id"],
                proposed_price=proposed_price,
                win_probability_pct=win_prob_pct,
                basis=basis,
                sample_count=metadata["sample_count"],
            ))
    return seeded


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed pricing baselines for active pursuits")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    try:
        seeded = await seed_pricing_baselines(pool, limit=args.limit)
        log.info("seeded %d pricing baselines", len(seeded))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
