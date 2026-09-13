"""Fetch GASTAT price indices from the DataSaudi public API into price_indices.

DataSaudi (datasaudi.sa) serves GASTAT's published statistics through a public
Tesseract OLAP API; no key or account is needed. Monthly series are fetched in
full each time (a few hundred rows), so revisions are picked up automatically.

Run:
  DATABASE_URL=... python -m thaqip_ingestion.price_indices [--min-hours 20]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date

import asyncpg
import httpx

from . import db

log = logging.getLogger("thaqip.price_indices")

API = os.environ.get("THAQIP_DATASAUDI_API", "https://api.datasaudi.sa/tesseract/data.jsonrecords")
CONNECTOR = "gastat.price_indices"


@dataclass(frozen=True)
class SeriesSpec:
    key: str
    cube: str
    drilldowns: str
    measure: str
    member_field: str
    member: str

    @property
    def source_series(self) -> str:
        return f"{self.cube}:{self.member}:{self.measure}"


SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec("cpi.general", "gastat_inflation_province_yoy", "Month,Nation",
               "Consumer Price Index", "Nation", "Saudi Arabia"),
    SeriesSpec("cpi.transport", "gastat_inflation", "Month,Main Division",
               "Inflation", "Main Division", "Transport"),
    SeriesSpec("cpi.housing_utilities", "gastat_inflation", "Month,Main Division",
               "Inflation", "Main Division", "Housing, Water, Electricity, Gas and other Fuels"),
    SeriesSpec("wpi.general", "gastat_wpi", "Month,Category",
               "Wholesale Price Index", "Category", "General index"),
    SeriesSpec("wpi.metal_machinery", "gastat_wpi", "Month,Category",
               "Wholesale Price Index", "Category", "Metal products, machinery and equipment"),
    SeriesSpec("wpi.ores_minerals", "gastat_wpi", "Month,Category",
               "Wholesale Price Index", "Category", "Ores and Minerals"),
    SeriesSpec("ppi.general", "producer_price_index_monthly", "Month,Economic Activity",
               "Producer Price Index", "Economic Activity", "General"),
    SeriesSpec("ppi.manufacturing", "producer_price_index_monthly", "Month,Economic Activity",
               "Producer Price Index", "Economic Activity", "Manufacturing"),
    SeriesSpec("cci.general", "construction_cost_index_by_sector", "Month,Sector",
               "Construction Cost Index", "Sector", "General Index"),
    SeriesSpec("cci.non_residential", "construction_cost_index_by_sector", "Month,Sector",
               "Construction Cost Index", "Sector", "Non-Residential Sector"),
)


def parse_month(text: str) -> date:
    year, month = str(text)[:7].split("-")
    return date(int(year), int(month), 1)


def extract(spec: SeriesSpec, payload: dict) -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    for row in payload.get("data") or []:
        if str(row.get(spec.member_field)) != spec.member:
            continue
        value = row.get(spec.measure)
        if value is None or not row.get("Month"):
            continue
        value = float(value)
        if value > 0:
            out.append((parse_month(row["Month"]), value))
    return sorted(out)


async def fetch_all(client: httpx.AsyncClient) -> dict[str, list[tuple[date, float]]]:
    cache: dict[tuple[str, str, str], dict] = {}
    result: dict[str, list[tuple[date, float]]] = {}
    for spec in SERIES:
        k = (spec.cube, spec.drilldowns, spec.measure)
        if k not in cache:
            r = await client.get(API, params={"cube": spec.cube, "drilldowns": spec.drilldowns,
                                              "measures": spec.measure})
            r.raise_for_status()
            cache[k] = r.json()
        result[spec.key] = extract(spec, cache[k])
    return result


async def store(pool: asyncpg.Pool, series: dict[str, list[tuple[date, float]]]) -> int:
    specs = {s.key: s for s in SERIES}
    rows = [(key, period, value, specs[key].source_series)
            for key, points in series.items() for period, value in points]
    if not rows:
        return 0
    await pool.executemany(
        """INSERT INTO price_indices (series, period, value, source_series)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (series, period) DO UPDATE
             SET value = EXCLUDED.value, source_series = EXCLUDED.source_series,
                 fetched_at = now()
           WHERE price_indices.value IS DISTINCT FROM EXCLUDED.value""",
        rows)
    return len(rows)


async def run(pool: asyncpg.Pool, *, min_hours: float = 0.0) -> dict:
    if min_hours:
        recent = await pool.fetchval(
            """SELECT EXISTS (SELECT 1 FROM ingest_runs WHERE connector = $1 AND ok
                              AND started_at > now() - make_interval(hours => $2))""",
            CONNECTOR, int(min_hours))
        if recent:
            return {"skipped": True}
    run_id = await pool.fetchval(
        "INSERT INTO ingest_runs (connector) VALUES ($1) RETURNING id", CONNECTOR)
    error = None
    stats: dict = {}
    try:
        async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "thaqip-indices/1.0"}) as client:
            series = await fetch_all(client)
        empty = [k for k, v in series.items() if not v]
        if "cpi.general" in empty:
            raise RuntimeError("cpi.general came back empty; refusing to store a partial refresh")
        stats = {"rows": await store(pool, series), "empty_series": empty,
                 "latest": {k: v[-1][0].isoformat() for k, v in series.items() if v}}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        await pool.execute(
            "UPDATE ingest_runs SET finished_at=now(), ok=$2, items_seen=$3, error=$4 WHERE id=$1",
            run_id, error is None, stats.get("rows", 0), error)
    return stats


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-hours", type=float, default=0.0,
                        help="skip if a successful fetch ran within this many hours")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    try:
        log.info("price indices: %s", await run(pool, min_hours=args.min_hours))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
