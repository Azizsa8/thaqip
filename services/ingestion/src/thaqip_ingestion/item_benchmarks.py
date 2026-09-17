"""Item price benchmark materialization (PRD "Thaqip for Contractors" §5.4,
T-BENCH-01/T-BENCH-02, T-FAKE-01).

Reads confirmed, consented BoQ line prices and writes pooled p25/p50/p75
percentiles per catalogue item, over a trailing 24-month contribution
window (the PRD's own wording: "an item shows a price range only when >= 5
distinct tenants contributed within 24 months"). item_benchmarks already has
a hard `CHECK (n_contributors >= 5)` (0022_boq_workbench.sql) so a
short-of-5 group is refused at the DB layer even if this code had a bug —
but the query below also filters with `HAVING count(DISTINCT tenant_id) >= 5`
so a short group is never even attempted, not merely rejected.

What counts as a contributing line, all enforced in one query, not spread
across app-layer checks that could be individually forgotten:
  - `match_confirmed_by IS NOT NULL` — a human confirmed the catalogue
    match (T-MATCH-01: unconfirmed lines never enter benchmarks).
  - An ACTIVE consent exists for the line's document — `boq_consents` with
    scope 'item_pool' and `withdrawn_at IS NULL`, not `boq_documents.
    consent_pool` alone, since withdrawal is tracked in boq_consents and
    deliberately does NOT flip that flag (see 0022's own column comment).
  - The document's upload date falls inside the trailing lookback window.

Each run's period_end is "now" — a benchmark row is a dated snapshot, not a
live-mutated aggregate (matches the consent-withdrawal terms already
promised in 0022: "already-published aggregates are not recomputed
retroactively"). Percentiles are computed in SQL via `percentile_cont`, the
same function this codebase already trusts for medians elsewhere (see
services/console/app.py's create_pursuit), rather than reimplementing
percentile interpolation in Python.

NOT BUILT: CPI/construction-cost-index time-adjustment (`index_used` is
always null here). The PRD ties this to GASTAT's construction cost index,
which needs >=24 months of history from a 2025-06 start — not enough
history exists yet, and applying the general CPI series to construction
item prices without evidence it transfers would be exactly the kind of
fabricated-precision this project avoids elsewhere.

Run (cron, e.g. nightly):
    DATABASE_URL=... uv run --extra db python -m thaqip_ingestion.item_benchmarks
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

import asyncpg

from . import db

log = logging.getLogger("thaqip.item_benchmarks")

LOOKBACK_DAYS = 730  # ~24 months
MIN_CONTRIBUTORS = 5

MATERIALIZE_SQL = """
SELECT l.catalogue_item_id,
       count(DISTINCT d.tenant_id) AS n_contributors,
       count(*) AS n_lines,
       percentile_cont(0.25) WITHIN GROUP (ORDER BY l.unit_price) AS p25,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY l.unit_price) AS p50,
       percentile_cont(0.75) WITHIN GROUP (ORDER BY l.unit_price) AS p75
FROM boq_lines l
JOIN boq_documents d ON d.id = l.document_id
JOIN boq_consents k ON k.document_id = d.id AND k.scope = 'item_pool' AND k.withdrawn_at IS NULL
WHERE l.catalogue_item_id IS NOT NULL
  AND l.match_confirmed_by IS NOT NULL
  AND l.unit_price IS NOT NULL
  AND d.created_at >= $1
GROUP BY l.catalogue_item_id
HAVING count(DISTINCT d.tenant_id) >= 5
"""

UPSERT_SQL = """
INSERT INTO item_benchmarks
    (catalogue_item_id, period_start, period_end, n_contributors, n_lines, p25, p50, p75,
     index_used, computed_at)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NULL, now())
ON CONFLICT (catalogue_item_id, period_start, period_end) DO UPDATE SET
    n_contributors = EXCLUDED.n_contributors,
    n_lines        = EXCLUDED.n_lines,
    p25            = EXCLUDED.p25,
    p50            = EXCLUDED.p50,
    p75            = EXCLUDED.p75,
    computed_at    = now()
"""


async def materialize_benchmarks(pool: asyncpg.Pool, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    period_start = now - timedelta(days=LOOKBACK_DAYS)
    rows = await pool.fetch(MATERIALIZE_SQL, period_start)
    for r in rows:
        assert r["n_contributors"] >= MIN_CONTRIBUTORS  # belt-and-suspenders with the query's own HAVING
        await pool.execute(
            UPSERT_SQL, r["catalogue_item_id"], period_start.date(), now.date(),
            r["n_contributors"], r["n_lines"], r["p25"], r["p50"], r["p75"],
        )
    return len(rows)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pool = await db.connect(os.environ["DATABASE_URL"])
    try:
        n = await materialize_benchmarks(pool)
        log.info("materialized benchmarks for %d catalogue items", n)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
