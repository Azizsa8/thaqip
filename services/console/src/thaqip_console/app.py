"""Thaqip internal console (ticket E1).

Read-only web UI over the live corpus: tender table with filters, tender
detail with offers/awards, freshness and corpus stats. Internal tooling —
auth comes with the staging deployment (A3); do not expose publicly.

Run:  DATABASE_URL=... uv run uvicorn thaqip_console.app:app --port 8080
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"], min_size=1, max_size=5
    )
    yield
    await app.state.pool.close()


app = FastAPI(title="Thaqip Console", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/stats")
async def stats():
    pool: asyncpg.Pool = app.state.pool
    counts = await pool.fetchrow(
        """SELECT
             (SELECT count(*) FROM tenders)                                        AS tenders,
             (SELECT count(*) FROM tenders WHERE last_offer_date > now())          AS open_tenders,
             (SELECT count(*) FROM agencies)                                       AS agencies,
             (SELECT count(*) FROM vendors)                                        AS vendors,
             (SELECT count(*) FROM offers)                                         AS offers,
             (SELECT count(*) FROM awards)                                         AS awards,
             (SELECT count(*) FROM ingest_events)                                  AS events,
             (SELECT count(*) FROM ingest_events WHERE relayed_at IS NULL)         AS unrelayed"""
    )
    fresh = await pool.fetchrow(
        """SELECT count(*) AS n,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY detected_at - published_at) AS p50
           FROM tenders
           WHERE published_at IS NOT NULL AND published_at > now() - interval '24 hours'
             AND detected_at >= published_at"""
    )
    return {
        **dict(counts),
        "fresh_24h": fresh["n"],
        "fresh_p50_seconds": fresh["p50"].total_seconds() if fresh["p50"] is not None else None,
    }


@app.get("/api/dashboard")
async def dashboard():
    """Real aggregates only — nothing invented. Feeds the dashboard view."""
    pool: asyncpg.Pool = app.state.pool
    kpis = await pool.fetchrow(
        """SELECT
             (SELECT count(*) FROM tenders WHERE last_offer_date > now())                    AS open_now,
             (SELECT count(*) FROM tenders WHERE last_offer_date::date = now()::date)       AS closing_today,
             (SELECT count(*) FROM tenders WHERE published_at > now() - interval '24 hours')AS new_24h,
             (SELECT count(*) FROM tenders)                                                 AS total,
             (SELECT coalesce(sum(award_value),0) FROM awards)                              AS awards_value,
             (SELECT count(*) FROM awards)                                                  AS awards_count,
             (SELECT count(*) FROM offers)                                                  AS offers_count,
             (SELECT count(DISTINCT vendor_id) FROM offers)                                 AS bidders,
             (SELECT max(award_value) FROM awards)                                          AS peak_award,
             (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY c) FROM
                (SELECT count(*) c FROM offers GROUP BY tender_id) s)                       AS median_bidders"""
    )
    fresh = await pool.fetchrow(
        """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY detected_at - published_at) AS p50
           FROM tenders WHERE published_at > now() - interval '24 hours'
             AND detected_at >= published_at"""
    )
    monthly = await pool.fetch(
        """SELECT to_char(date_trunc('month', t.last_offer_date), 'YYYY-MM') AS month,
                  count(*) FILTER (WHERE o.is_winner)          AS won,
                  count(*) FILTER (WHERE NOT o.is_winner)      AS lost
           FROM offers o JOIN tenders t ON t.id = o.tender_id
           WHERE t.last_offer_date IS NOT NULL
           GROUP BY 1 ORDER BY 1 DESC LIMIT 12"""
    )
    latest_awards = await pool.fetch(
        """SELECT t.id, left(t.name, 70) AS name,
                  coalesce(a2.canonical_name, t.agency_name_raw) AS agency,
                  v.canonical_name AS winner, w.award_value
           FROM awards w
           JOIN tenders t ON t.id = w.tender_id
           LEFT JOIN vendors v ON v.id = w.vendor_id
           LEFT JOIN agencies a2 ON a2.id = t.agency_id
           ORDER BY w.id DESC LIMIT 8"""
    )
    return {
        **dict(kpis),
        "fresh_p50_seconds": fresh["p50"].total_seconds() if fresh and fresh["p50"] else None,
        "monthly": [dict(r) for r in reversed(monthly)],
        "latest_awards": [dict(r) for r in latest_awards],
    }


@app.get("/api/filters")
async def filters():
    pool: asyncpg.Pool = app.state.pool
    agencies = await pool.fetch(
        """SELECT a.id, a.canonical_name, count(t.id) AS n
           FROM agencies a JOIN tenders t ON t.agency_id = a.id
           GROUP BY a.id ORDER BY n DESC LIMIT 100"""
    )
    activities = await pool.fetch(
        """SELECT activity_id AS id, activity_name_raw AS name, count(*) AS n
           FROM tenders WHERE activity_id IS NOT NULL
           GROUP BY 1, 2 ORDER BY n DESC LIMIT 50"""
    )
    return {
        "agencies": [dict(r) for r in agencies],
        "activities": [dict(r) for r in activities],
    }


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    channel: str = "log"                       # log | telegram | email
    target: str = "dev-console"
    keywords: list[str] = []
    activity_ids: list[int] = []
    agency_ids: list[int] = []
    sources: list[str] = ["etimad", "forsah"]
    event_types: list[str] = ["tender.created", "tender.extended", "tender.awarded"]


@app.get("/api/profiles")
async def profiles():
    rows = await app.state.pool.fetch(
        """SELECT p.*,
                  (SELECT count(*) FROM notifications n WHERE n.profile_id = p.id) AS sent_count,
                  (SELECT max(created_at) FROM notifications n WHERE n.profile_id = p.id) AS last_at
           FROM alert_profiles p ORDER BY p.id DESC"""
    )
    return [dict(r) for r in rows]


@app.post("/api/profiles")
async def create_profile(p: ProfileIn):
    row = await app.state.pool.fetchrow(
        """INSERT INTO alert_profiles (name, channel, target, keywords, activity_ids,
                                       agency_ids, sources, event_types)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
        p.name, p.channel, p.target, p.keywords, p.activity_ids,
        p.agency_ids, p.sources, p.event_types,
    )
    return {"id": row["id"]}


@app.patch("/api/profiles/{pid}/toggle")
async def toggle_profile(pid: int):
    active = await app.state.pool.fetchval(
        "UPDATE alert_profiles SET active = NOT active WHERE id=$1 RETURNING active", pid
    )
    if active is None:
        raise HTTPException(404)
    return {"active": active}


@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: int):
    await app.state.pool.execute("DELETE FROM notifications WHERE profile_id=$1", pid)
    n = await app.state.pool.execute("DELETE FROM alert_profiles WHERE id=$1", pid)
    if n.endswith("0"):
        raise HTTPException(404)
    return {"deleted": True}


@app.get("/api/notifications")
async def notifications(limit: int = Query(50, le=200)):
    rows = await app.state.pool.fetch(
        """SELECT n.id, n.event_type, n.channel, n.status, n.title, n.body, n.created_at,
                  n.tender_id, p.name AS profile_name
           FROM notifications n JOIN alert_profiles p ON p.id = n.profile_id
           ORDER BY n.id DESC LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


@app.get("/api/tenders")
async def tenders(
    q: str | None = None,
    agency_id: int | None = None,
    activity_id: int | None = None,
    awarded: bool | None = None,
    open_only: bool = False,
    source: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    pool: asyncpg.Pool = app.state.pool
    where, args = ["TRUE"], []

    def arg(v):
        args.append(v)
        return f"${len(args)}"

    if q:
        where.append(f"(t.name ILIKE {arg('%' + q + '%')} OR t.reference_number = {arg(q)})")
    if agency_id:
        where.append(f"t.agency_id = {arg(agency_id)}")
    if activity_id:
        where.append(f"t.activity_id = {arg(activity_id)}")
    if awarded is True:
        where.append("EXISTS (SELECT 1 FROM awards w WHERE w.tender_id = t.id)")
    if open_only:
        where.append("t.last_offer_date > now()")
    if source in ("etimad", "forsah"):
        where.append(f"t.source = {arg(source)}")

    rows = await pool.fetch(
        f"""SELECT t.id, t.source, t.source_tender_id, t.reference_number, t.name,
                   t.submitted_bids_count, t.draft_bids_count, t.external_bids_count,
                   coalesce(a.canonical_name, t.agency_name_raw) AS agency,
                   t.activity_name_raw AS activity, t.status_id,
                   t.last_offer_date, t.published_at, t.detected_at,
                   EXISTS (SELECT 1 FROM awards w WHERE w.tender_id = t.id) AS has_award,
                   greatest(0, extract(epoch FROM t.last_offer_date - now()))::bigint AS remaining_s
            FROM tenders t LEFT JOIN agencies a ON a.id = t.agency_id
            WHERE {' AND '.join(where)}
            ORDER BY t.published_at DESC NULLS LAST
            LIMIT {arg(limit)} OFFSET {arg(offset)}""",
        *args,
    )
    total = await pool.fetchval(
        f"SELECT count(*) FROM tenders t WHERE {' AND '.join(where[: len(where)])}",
        *args[:-2],
    )
    return {"total": total, "items": [dict(r) for r in rows]}


@app.get("/api/tenders/{tender_id}")
async def tender_detail(tender_id: int):
    pool: asyncpg.Pool = app.state.pool
    t = await pool.fetchrow(
        """SELECT t.*, coalesce(a.canonical_name, t.agency_name_raw) AS agency
           FROM tenders t LEFT JOIN agencies a ON a.id = t.agency_id WHERE t.id = $1""",
        tender_id,
    )
    if t is None:
        raise HTTPException(404)
    offers = await pool.fetch(
        """SELECT v.canonical_name AS vendor, o.offer_value, o.is_winner, o.technical_pass
           FROM offers o LEFT JOIN vendors v ON v.id = o.vendor_id
           WHERE o.tender_id = $1 ORDER BY o.offer_value NULLS LAST""",
        tender_id,
    )
    awards = await pool.fetch(
        """SELECT v.canonical_name AS vendor, w.award_value
           FROM awards w LEFT JOIN vendors v ON v.id = w.vendor_id WHERE w.tender_id = $1""",
        tender_id,
    )
    boq = await pool.fetch(
        """SELECT item_no, description, unit, qty, confidence
           FROM boq_items WHERE tender_id = $1 ORDER BY id LIMIT 500""",
        tender_id,
    )
    docs = await pool.fetch(
        """SELECT id, kind, file_name, mime_type, size_bytes, text_extracted
           FROM documents WHERE tender_id = $1 ORDER BY id""",
        tender_id,
    )
    out = dict(t)
    out.pop("payload", None)
    out["offers"] = [dict(r) for r in offers]
    out["awards"] = [dict(r) for r in awards]
    out["boq_items"] = [dict(r) for r in boq]
    out["documents"] = [dict(r) for r in docs]
    return out
