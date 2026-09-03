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
           WHERE detected_by = 'poller' AND published_at IS NOT NULL AND published_at > now() - interval '24 hours'
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
             (SELECT count(*) FROM tenders WHERE detected_by = 'poller' AND published_at > now() - interval '24 hours')AS new_24h,
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
           FROM tenders WHERE detected_by = 'poller' AND published_at > now() - interval '24 hours'
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


GTPL_BASELINE = [
    ("سجل تجاري ساري المفعول", "document", "نظام المنافسات — متطلبات التأهيل", 10),
    ("شهادة تسديد الزكاة والدخل سارية", "document", "نظام المنافسات — شروط تقديم العطاء", 11),
    ("شهادة اشتراك الغرفة التجارية سارية", "document", "نظام المنافسات — شروط تقديم العطاء", 12),
    ("شهادة التأمينات الاجتماعية (GOSI)", "document", "متطلبات التأهيل النظامية", 13),
    ("شهادة السعودة / نطاقات", "document", "متطلبات التأهيل النظامية", 14),
    ("خطاب تقديم موقّع بالإقرار بالاطلاع على كراسة الشروط", "document", "نظام المنافسات — شروط تقديم العطاء", 15),
    ("بيان الأعمال السابقة المماثلة", "qualification", "نظام المنافسات — تقييم القدرات", 20),
]


def _field_requirements(t: dict) -> list[tuple[str, str, str, int]]:
    items = []
    if t.get("last_enquiries_date"):
        items.append((f"إرسال الاستفسارات قبل {str(t['last_enquiries_date'])[:10]}",
                      "deadline", "بيانات المنافسة — آخر موعد للاستفسارات", 1))
    if t.get("last_offer_date"):
        items.append((f"تقديم العرض قبل {str(t['last_offer_date'])[:16]}",
                      "deadline", "بيانات المنافسة — آخر موعد لتقديم العروض", 2))
    if t.get("booklet_price") and float(t["booklet_price"]) > 0:
        items.append((f"شراء كراسة الشروط ({float(t['booklet_price']):,.0f} ر.س) عبر اعتماد",
                      "document", "بيانات المنافسة — قيمة الكراسة", 5))
    if t.get("source") == "etimad" and t.get("tender_type_id") == 1:
        items.append(("ضمان ابتدائي بنسبة 1%–2% من قيمة العطاء (ما لم تُعفِ الكراسة)",
                      "guarantee", "نظام المنافسات — الضمان الابتدائي (منافسة عامة)", 30))
    if any(k in (t.get("activity_name_raw") or "") for k in ("إنشاء", "مقاول", "تشييد", "بناء")):
        items.append(("شهادة تصنيف المقاولين في المجال والدرجة المطلوبة",
                      "qualification", "اشتراط التصنيف لأنشطة الإنشاءات", 21))
    return items


class PursuitIn(BaseModel):
    tender_id: int


@app.post("/api/pursuits")
async def create_pursuit(body: PursuitIn):
    pool: asyncpg.Pool = app.state.pool
    t = await pool.fetchrow("SELECT * FROM tenders WHERE id=$1", body.tender_id)
    if t is None:
        raise HTTPException(404, "tender not found")
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval(
                "SELECT id FROM pursuits WHERE tender_id=$1", body.tender_id)
            if existing:
                return {"id": existing, "created": False}
            pid = await conn.fetchval(
                "INSERT INTO pursuits (tender_id) VALUES ($1) RETURNING id", body.tender_id)
            for req, cat, ref, order in _field_requirements(dict(t)) + GTPL_BASELINE:
                await conn.execute(
                    """INSERT INTO compliance_items
                         (pursuit_id, requirement, category, source_ref, origin, sort_order)
                       VALUES ($1,$2,$3,$4,'rule',$5)""", pid, req, cat, ref, order)
    return {"id": pid, "created": True}


@app.get("/api/pursuits")
async def pursuits():
    rows = await app.state.pool.fetch(
        """SELECT p.id, p.stage, p.created_at, t.id AS tender_id, t.name, t.reference_number,
                  t.source, coalesce(a.canonical_name, t.agency_name_raw) AS agency,
                  t.last_offer_date,
                  greatest(0, extract(epoch FROM t.last_offer_date - now()))::bigint AS remaining_s,
                  (SELECT count(*) FROM compliance_items c WHERE c.pursuit_id = p.id) AS items,
                  (SELECT count(*) FROM compliance_items c
                    WHERE c.pursuit_id = p.id AND c.status IN ('met','n_a'))          AS items_done
           FROM pursuits p
           JOIN tenders t ON t.id = p.tender_id
           LEFT JOIN agencies a ON a.id = t.agency_id
           ORDER BY p.created_at DESC"""
    )
    return [dict(r) for r in rows]


@app.get("/api/pursuits/{pid}")
async def pursuit_detail(pid: int):
    pool: asyncpg.Pool = app.state.pool
    p = await pool.fetchrow(
        """SELECT p.*, t.name, t.reference_number, t.source,
                  coalesce(a.canonical_name, t.agency_name_raw) AS agency,
                  t.last_offer_date, t.last_offer_date_hijri,
                  greatest(0, extract(epoch FROM t.last_offer_date - now()))::bigint AS remaining_s
           FROM pursuits p JOIN tenders t ON t.id = p.tender_id
           LEFT JOIN agencies a ON a.id = t.agency_id WHERE p.id=$1""", pid)
    if p is None:
        raise HTTPException(404)
    items = await pool.fetch(
        "SELECT * FROM compliance_items WHERE pursuit_id=$1 ORDER BY sort_order, id", pid)
    out = dict(p)
    out["compliance"] = [dict(r) for r in items]
    return out


class ItemPatch(BaseModel):
    status: str  # missing | in_progress | met | n_a


@app.patch("/api/compliance/{item_id}")
async def patch_item(item_id: int, body: ItemPatch):
    if body.status not in ("missing", "in_progress", "met", "n_a"):
        raise HTTPException(422)
    n = await app.state.pool.execute(
        "UPDATE compliance_items SET status=$2 WHERE id=$1", item_id, body.status)
    if n.endswith("0"):
        raise HTTPException(404)
    return {"ok": True}


class StagePatch(BaseModel):
    stage: str


@app.patch("/api/pursuits/{pid}/stage")
async def patch_stage(pid: int, body: StagePatch):
    if body.stage not in ("studying", "pricing", "writing", "submitted", "won", "lost"):
        raise HTTPException(422)
    n = await app.state.pool.execute(
        "UPDATE pursuits SET stage=$2, updated_at=now() WHERE id=$1", pid, body.stage)
    if n.endswith("0"):
        raise HTTPException(404)
    return {"ok": True}


class OutcomeIn(BaseModel):
    result: str                      # won | lost
    submitted_value: float | None = None
    notes: str | None = None


@app.post("/api/pursuits/{pid}/outcome")
async def log_outcome(pid: int, body: OutcomeIn):
    """M5-1: capture bid outcome; auto-reconcile award value + competitor count."""
    if body.result not in ("won", "lost"):
        raise HTTPException(422)
    pool: asyncpg.Pool = app.state.pool
    tender_id = await pool.fetchval("SELECT tender_id FROM pursuits WHERE id=$1", pid)
    if tender_id is None:
        raise HTTPException(404)
    award_value = await pool.fetchval(
        "SELECT award_value FROM awards WHERE tender_id=$1 ORDER BY id LIMIT 1", tender_id)
    competitor_count = await pool.fetchval(
        "SELECT count(*) FROM offers WHERE tender_id=$1", tender_id) or None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO outcomes (pursuit_id, result, submitted_value, award_value,
                                         competitor_count, notes)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT (pursuit_id) DO UPDATE SET result=EXCLUDED.result,
                     submitted_value=EXCLUDED.submitted_value, notes=EXCLUDED.notes,
                     award_value=EXCLUDED.award_value, competitor_count=EXCLUDED.competitor_count,
                     logged_at=now()""",
                pid, body.result, body.submitted_value, award_value, competitor_count, body.notes)
            await conn.execute(
                "UPDATE pursuits SET stage=$2, updated_at=now() WHERE id=$1", pid, body.result)
            await conn.execute(
                """INSERT INTO ingest_events (event_type, entity_type, entity_id, data)
                   VALUES ('outcome.logged', 'pursuit', $1, '{}')""", pid)
    return {"ok": True, "award_value": float(award_value) if award_value else None,
            "competitor_count": competitor_count}


@app.get("/api/outcomes/summary")
async def outcomes_summary():
    row = await app.state.pool.fetchrow(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE result='won') AS won,
                  avg(submitted_value) FILTER (WHERE result='won') AS avg_win_value
           FROM outcomes""")
    return dict(row)


@app.get("/api/vendors")
async def vendors(q: str | None = None, limit: int = Query(50, le=200)):
    """M8-1: competitor leaderboard from the harvested offers/awards corpus."""
    pool: asyncpg.Pool = app.state.pool
    where, args = "", []
    if q:
        args.append(f"%{q}%")
        where = "WHERE v.canonical_name ILIKE $1"
    rows = await pool.fetch(f"""
        SELECT v.id, v.canonical_name,
               count(o.id)                                   AS participations,
               count(o.id) FILTER (WHERE o.is_winner)        AS wins,
               round(100.0*count(o.id) FILTER (WHERE o.is_winner)/nullif(count(o.id),0),1) AS win_rate,
               round(avg(o.offer_value),0)                   AS avg_offer,
               coalesce(sum(w.award_value),0)                AS awarded_value,
               count(o.id) FILTER (WHERE o.technical_pass)   AS tech_pass
        FROM vendors v
        JOIN offers o ON o.vendor_id = v.id
        LEFT JOIN awards w ON w.vendor_id = v.id AND w.tender_id = o.tender_id AND o.is_winner
        {where}
        GROUP BY v.id
        ORDER BY wins DESC, participations DESC
        LIMIT {int(limit)}""", *args)
    return [dict(r) for r in rows]


@app.get("/api/vendors/{vid}")
async def vendor_detail(vid: int):
    pool: asyncpg.Pool = app.state.pool
    v = await pool.fetchrow("SELECT * FROM vendors WHERE id=$1", vid)
    if v is None:
        raise HTTPException(404)
    stats = await pool.fetchrow("""
        SELECT count(*) AS participations,
               count(*) FILTER (WHERE o.is_winner) AS wins,
               round(avg(o.offer_value),0) AS avg_offer,
               min(o.offer_value) AS min_offer, max(o.offer_value) AS max_offer,
               round(100.0*count(*) FILTER (WHERE o.technical_pass)/nullif(count(*),0),1) AS tech_rate
        FROM offers o WHERE o.vendor_id=$1""", vid)
    history = await pool.fetch("""
        SELECT t.id AS tender_id, left(t.name,70) AS tender_name,
               coalesce(a.canonical_name, t.agency_name_raw) AS agency,
               t.activity_name_raw AS activity,
               o.offer_value, o.is_winner, o.technical_pass,
               (SELECT min(o2.offer_value) FROM offers o2
                 WHERE o2.tender_id=t.id AND o2.technical_pass) AS lowest_offer
        FROM offers o
        JOIN tenders t ON t.id = o.tender_id
        LEFT JOIN agencies a ON a.id = t.agency_id
        WHERE o.vendor_id=$1 ORDER BY o.id DESC LIMIT 100""", vid)
    agencies = await pool.fetch("""
        SELECT coalesce(a.canonical_name, t.agency_name_raw) AS agency, count(*) AS n,
               count(*) FILTER (WHERE o.is_winner) AS wins
        FROM offers o JOIN tenders t ON t.id=o.tender_id
        LEFT JOIN agencies a ON a.id=t.agency_id
        WHERE o.vendor_id=$1 GROUP BY 1 ORDER BY n DESC LIMIT 8""", vid)
    out = dict(v)
    out["stats"] = dict(stats)
    out["history"] = [dict(r) for r in history]
    out["agencies"] = [dict(r) for r in agencies]
    return out


TYPESENSE_URL = os.environ.get("TYPESENSE_URL", "http://localhost:8108")
TYPESENSE_KEY = os.environ.get("TYPESENSE_KEY", "thaqip_dev_search")


@app.get("/api/search")
async def search(q: str, limit: int = Query(50, le=100), offset: int = 0,
                 source: str | None = None, awarded: bool | None = None,
                 open_only: bool = False):
    """E2: full-text search over names, agencies, activities AND document text."""
    import httpx

    filters = []
    if source in ("etimad", "forsah"):
        filters.append(f"source:={source}")
    if awarded:
        filters.append("has_award:=true")
    if open_only:
        filters.append("open_now:=true")
    params = {
        "q": q, "query_by": "name,reference,agency,activity,doc_text",
        "query_by_weights": "10,10,4,4,2",
        "per_page": limit, "page": offset // limit + 1,
        "highlight_fields": "doc_text", "highlight_affix_num_tokens": 6,
    }
    if filters:
        params["filter_by"] = " && ".join(filters)
    async with httpx.AsyncClient(timeout=15) as h:
        r = await h.get(f"{TYPESENSE_URL}/collections/tenders/documents/search",
                        params=params, headers={"X-TYPESENSE-API-KEY": TYPESENSE_KEY})
    if r.status_code != 200:
        raise HTTPException(503, "search index unavailable")
    res = r.json()
    hits = res.get("hits", [])
    ids = [int(h_["document"]["id"]) for h_ in hits]
    snippets = {}
    for h_ in hits:
        for hl in h_.get("highlights", []):
            if hl.get("field") == "doc_text":
                snippets[int(h_["document"]["id"])] = hl.get("snippet", "")
    if not ids:
        return {"total": 0, "items": []}
    pool: asyncpg.Pool = app.state.pool
    rows = await pool.fetch(
        """SELECT t.id, t.source, t.reference_number, t.name,
                  t.submitted_bids_count, t.draft_bids_count,
                  coalesce(a.canonical_name, t.agency_name_raw) AS agency,
                  t.activity_name_raw AS activity, t.status_id,
                  t.last_offer_date, t.published_at, t.detected_at,
                  EXISTS (SELECT 1 FROM awards w WHERE w.tender_id = t.id) AS has_award,
                  greatest(0, extract(epoch FROM t.last_offer_date - now()))::bigint AS remaining_s
           FROM tenders t LEFT JOIN agencies a ON a.id = t.agency_id
           WHERE t.id = ANY($1)""", ids)
    by_id = {r_["id"]: dict(r_) for r_ in rows}
    items = []
    for i in ids:
        if i in by_id:
            item = by_id[i]
            if i in snippets:
                item["snippet"] = snippets[i]
            items.append(item)
    return {"total": res.get("found", len(items)), "items": items}


@app.get("/api/agencies")
async def agencies_board(q: str | None = None, limit: int = Query(50, le=200)):
    """M8-2: agency behavior leaderboard."""
    pool: asyncpg.Pool = app.state.pool
    where, args = "", []
    if q:
        args.append(f"%{q}%")
        where = "WHERE a.canonical_name ILIKE $1"
    rows = await pool.fetch(f"""
        SELECT a.id, a.canonical_name,
               count(t.id)                                          AS tenders,
               count(t.id) FILTER (WHERE t.last_offer_date > now()) AS open_now,
               count(w.id)                                          AS awards,
               coalesce(sum(w.award_value),0)                       AS awarded_value
        FROM agencies a
        JOIN tenders t ON t.agency_id = a.id
        LEFT JOIN awards w ON w.tender_id = t.id
        {where}
        GROUP BY a.id ORDER BY tenders DESC LIMIT {int(limit)}""", *args)
    return [dict(r) for r in rows]


@app.get("/api/agencies/{aid}")
async def agency_detail(aid: int):
    pool: asyncpg.Pool = app.state.pool
    a = await pool.fetchrow("SELECT * FROM agencies WHERE id=$1", aid)
    if a is None:
        raise HTTPException(404)
    stats = await pool.fetchrow("""
        SELECT count(t.id) AS tenders,
               count(t.id) FILTER (WHERE t.last_offer_date > now()) AS open_now,
               count(DISTINCT w.id) AS awards,
               coalesce(sum(w.award_value),0) AS awarded_value,
               round(avg(bid.n) FILTER (WHERE bid.n > 0),1) AS avg_bidders
        FROM tenders t
        LEFT JOIN awards w ON w.tender_id = t.id
        LEFT JOIN LATERAL (SELECT count(*) n FROM offers o WHERE o.tender_id=t.id AND
                           EXISTS (SELECT 1 FROM awards w2 WHERE w2.tender_id=t.id)) bid ON true
        WHERE t.agency_id=$1""", aid)
    top_vendors = await pool.fetch("""
        SELECT v.id, v.canonical_name, count(*) AS wins, sum(w.award_value) AS value
        FROM awards w JOIN tenders t ON t.id=w.tender_id JOIN vendors v ON v.id=w.vendor_id
        WHERE t.agency_id=$1 GROUP BY v.id ORDER BY wins DESC, value DESC LIMIT 6""", aid)
    activities = await pool.fetch("""
        SELECT activity_name_raw AS activity, count(*) AS n
        FROM tenders WHERE agency_id=$1 AND activity_name_raw IS NOT NULL
        GROUP BY 1 ORDER BY n DESC LIMIT 6""", aid)
    recent = await pool.fetch("""
        SELECT t.id, left(t.name,70) AS name, t.last_offer_date,
               greatest(0, extract(epoch FROM t.last_offer_date - now()))::bigint AS remaining_s,
               EXISTS (SELECT 1 FROM awards w WHERE w.tender_id=t.id) AS has_award,
               (SELECT w.award_value FROM awards w WHERE w.tender_id=t.id LIMIT 1) AS award_value
        FROM tenders t WHERE t.agency_id=$1 ORDER BY t.published_at DESC NULLS LAST LIMIT 15""", aid)
    out = dict(a)
    out["stats"] = dict(stats)
    out["top_vendors"] = [dict(r) for r in top_vendors]
    out["activities"] = [dict(r) for r in activities]
    out["recent"] = [dict(r) for r in recent]
    return out


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
    # M4-2 lite: pricing benchmark from awarded tenders in the same activity
    # (same agency ranked first). Vector similarity replaces this ranking later.
    similar = await pool.fetch(
        """SELECT t2.id, left(t2.name, 70) AS name,
                  coalesce(a2.canonical_name, t2.agency_name_raw) AS agency,
                  w.award_value, v.canonical_name AS winner,
                  (SELECT count(*) FROM offers o WHERE o.tender_id = t2.id) AS bidders,
                  (t2.agency_name_raw = $3 OR t2.agency_id IS NOT DISTINCT FROM $4) AS same_agency
           FROM awards w
           JOIN tenders t2 ON t2.id = w.tender_id
           LEFT JOIN vendors v ON v.id = w.vendor_id
           LEFT JOIN agencies a2 ON a2.id = t2.agency_id
           WHERE t2.id <> $1 AND t2.activity_id IS NOT DISTINCT FROM $2
             AND w.award_value IS NOT NULL
           ORDER BY same_agency DESC, w.id DESC LIMIT 6""",
        tender_id, t["activity_id"], t["agency_name_raw"], t["agency_id"],
    )
    bench = await pool.fetchrow(
        """SELECT count(*) AS n,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY w.award_value) AS median_award,
                  min(w.award_value) AS min_award, max(w.award_value) AS max_award
           FROM awards w JOIN tenders t2 ON t2.id = w.tender_id
           WHERE t2.id <> $1 AND t2.activity_id IS NOT DISTINCT FROM $2
             AND w.award_value IS NOT NULL""",
        tender_id, t["activity_id"],
    )
    out = dict(t)
    out.pop("payload", None)
    out["offers"] = [dict(r) for r in offers]
    out["awards"] = [dict(r) for r in awards]
    out["boq_items"] = [dict(r) for r in boq]
    out["documents"] = [dict(r) for r in docs]
    # Win-probability v1 (M5-3): honest baseline from historical competition
    # density in the same activity. For Forsah, live bid counters refine it.
    comp = await pool.fetchrow(
        """SELECT count(*) AS awarded_n,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY b.n) AS median_bidders,
                  percentile_cont(0.9) WITHIN GROUP (ORDER BY b.n) AS p90_bidders
           FROM tenders t2
           JOIN LATERAL (SELECT count(*) n FROM offers o WHERE o.tender_id=t2.id) b ON b.n > 0
           WHERE t2.activity_id IS NOT DISTINCT FROM $1
             AND EXISTS (SELECT 1 FROM awards w2 WHERE w2.tender_id = t2.id)""",
        t["activity_id"],
    )
    competition = None
    live_bidders = (t.get("submitted_bids_count") or 0) + (t.get("external_bids_count") or 0)
    if t["source"] == "forsah" and live_bidders > 0:
        competition = {"basis": "live", "expected_bidders": live_bidders,
                       "baseline_win_pct": round(100 / (live_bidders + 1), 1), "n": None}
    elif comp and comp["awarded_n"] and comp["median_bidders"]:
        mb = float(comp["median_bidders"])
        competition = {"basis": "activity_history", "expected_bidders": round(mb, 1),
                       "p90_bidders": round(float(comp["p90_bidders"]), 1),
                       "baseline_win_pct": round(100 / max(mb, 1), 1),
                       "n": comp["awarded_n"]}
    out["similar_awards"] = [dict(r) for r in similar]
    out["benchmark"] = dict(bench) if bench and bench["n"] else None
    out["competition"] = competition
    return out
