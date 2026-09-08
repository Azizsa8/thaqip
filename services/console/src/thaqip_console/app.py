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


async def _forsah_enrichment(source_uid: str) -> tuple[list[tuple[str, str, str, int]], list[str]]:
    """Forsah publicly declares required documents AND line items per
    opportunity — real compliance + BOQ data, no extraction needed.
    Returns (compliance_rows, boq_item_names); best-effort, short timeout."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=8) as h:
            r = await h.get(
                f"https://forsah-api.910ths.sa/api/v1/opportunities/{source_uid}",
                headers={"Accept": "application/json"})
        if r.status_code != 200:
            return [], []
        d = r.json()
        items = []
        for i, doc in enumerate((d.get("requiredGlobalDocuments") or [])
                                + (d.get("requiredCustomDocuments") or [])):
            name = (doc.get("name") or {}).get("ar") if isinstance(doc.get("name"), dict) \
                else (doc.get("name") or doc.get("nameAr") or "")
            if name:
                items.append((f"وثيقة مطلوبة: {name}", "document",
                              "متطلبات فرصة المعلنة للفرصة", 40 + i))
        if d.get("referenceNumber"):
            items.append((f"مرجع الفرصة الرسمي: {d['referenceNumber']}", "general",
                          "بيانات فرصة", 99))
        boq = [it.get("name") for it in (d.get("items") or []) if it.get("name")]
        return items, boq
    except Exception:  # noqa: BLE001 — enrichment must never block pursuit creation
        return [], []


@app.post("/api/pursuits")
async def create_pursuit(body: PursuitIn):
    pool: asyncpg.Pool = app.state.pool
    t = await pool.fetchrow("SELECT * FROM tenders WHERE id=$1", body.tender_id)
    if t is None:
        raise HTTPException(404, "tender not found")
    extra: list[tuple[str, str, str, int]] = []
    forsah_boq: list[str] = []
    if t["source"] == "forsah" and t["source_uid"]:
        extra, forsah_boq = await _forsah_enrichment(t["source_uid"])
    async with pool.acquire() as conn, conn.transaction():
        existing = await conn.fetchval(
            "SELECT id FROM pursuits WHERE tender_id=$1", body.tender_id)
        if existing:
            return {"id": existing, "created": False}
        pid = await conn.fetchval(
            "INSERT INTO pursuits (tender_id) VALUES ($1) RETURNING id", body.tender_id)
        base = _field_requirements(dict(t)) + (GTPL_BASELINE if t["source"] == "etimad" else [])
        for req, cat, ref, order in base + extra:
            await conn.execute(
                """INSERT INTO compliance_items
                     (pursuit_id, requirement, category, source_ref, origin, sort_order)
                   VALUES ($1,$2,$3,$4,'rule',$5)""", pid, req, cat, ref, order)
        for i, name in enumerate(forsah_boq, 1):
            await conn.execute(
                """INSERT INTO boq_items (tender_id, item_no, description, confidence)
                   VALUES ($1, $2, $3, 1.0)
                   ON CONFLICT DO NOTHING""",
                body.tender_id, str(i), name)
        # M5-4 groundwork: snapshot the baseline win prediction at decision time
        live = (t["submitted_bids_count"] or 0) + (t["external_bids_count"] or 0)
        if t["source"] == "forsah" and live > 0:
            await conn.execute(
                """INSERT INTO predictions (pursuit_id, value, basis)
                   VALUES ($1, $2, $3::jsonb)""",
                pid, round(1 / (live + 1), 4),
                f'{{"basis":"live","bidders":{live},"source":"forsah"}}')
        else:
            mb = await conn.fetchval(
                """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY b.n)
                   FROM tenders t2
                   JOIN LATERAL (SELECT count(*) n FROM offers o WHERE o.tender_id=t2.id) b ON b.n > 0
                   WHERE t2.activity_id IS NOT DISTINCT FROM $1
                     AND EXISTS (SELECT 1 FROM awards w2 WHERE w2.tender_id = t2.id)""",
                t["activity_id"])
            if mb:
                await conn.execute(
                    """INSERT INTO predictions (pursuit_id, value, basis)
                       VALUES ($1, $2, $3::jsonb)""",
                    pid, round(1 / max(float(mb), 1), 4),
                    f'{{"basis":"activity_history","median_bidders":{float(mb)}}}')
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
                  t.activity_id, t.activity_name_raw, t.submitted_bids_count,
                  t.external_bids_count, t.booklet_price,
                  coalesce(a.canonical_name, t.agency_name_raw) AS agency,
                  t.last_offer_date, t.last_offer_date_hijri,
                  greatest(0, extract(epoch FROM t.last_offer_date - now()))::bigint AS remaining_s
           FROM pursuits p JOIN tenders t ON t.id = p.tender_id
           LEFT JOIN agencies a ON a.id = t.agency_id WHERE p.id=$1""", pid)
    if p is None:
        raise HTTPException(404)
    items = await pool.fetch(
        "SELECT * FROM compliance_items WHERE pursuit_id=$1 ORDER BY sort_order, id", pid)
    market = await pool.fetchrow(
        """SELECT count(w.id)::int AS award_samples,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY w.award_value)::float AS median_award,
                  percentile_cont(0.25) WITHIN GROUP (ORDER BY w.award_value)::float AS p25_award,
                  percentile_cont(0.75) WITHIN GROUP (ORDER BY w.award_value)::float AS p75_award
           FROM awards w
           JOIN tenders t2 ON t2.id = w.tender_id
           WHERE t2.activity_id IS NOT DISTINCT FROM $1
             AND w.award_value IS NOT NULL""",
        p["activity_id"],
    )
    last_sim = await pool.fetchrow(
        """SELECT proposed_price::float AS proposed_price, win_pct::float AS win_pct,
                  expected_value::float AS expected_value, basis, created_at
           FROM pursuit_simulations
           WHERE pursuit_id=$1 ORDER BY created_at DESC LIMIT 1""",
        pid,
    )
    market_out = dict(market) if market else {"award_samples": 0}
    market_out["last_simulation"] = dict(last_sim) if last_sim else None
    market_out["last_win_pct"] = last_sim["win_pct"] if last_sim else None
    compliance = [dict(r) for r in items]
    out = dict(p)
    out["compliance"] = compliance
    out["decision"] = _war_room_decision(out, compliance, market_out)
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


async def _measure_pricing_predictions(conn: asyncpg.Connection, pursuit_id: int) -> int:
    """Attach actual award values to stored pricing simulations once known."""
    rows = await conn.fetch(
        """WITH actual AS (
               SELECT p.id AS pursuit_id, w.award_value::numeric AS award_value
               FROM pursuits p
               JOIN awards w ON w.tender_id = p.tender_id
               WHERE p.id = $1 AND w.award_value IS NOT NULL
               ORDER BY w.id LIMIT 1
             )
           INSERT INTO pricing_prediction_results
             (simulation_id, pursuit_id, actual_award_value, predicted_price,
              absolute_error, percentage_error)
           SELECT s.id, s.pursuit_id, actual.award_value, s.proposed_price,
                  abs(s.proposed_price - actual.award_value),
                  (abs(s.proposed_price - actual.award_value) / nullif(actual.award_value, 0))::real
           FROM pursuit_simulations s
           JOIN actual ON actual.pursuit_id = s.pursuit_id
           WHERE s.pursuit_id = $1
           ON CONFLICT (simulation_id) DO UPDATE SET
             actual_award_value = EXCLUDED.actual_award_value,
             predicted_price = EXCLUDED.predicted_price,
             absolute_error = EXCLUDED.absolute_error,
             percentage_error = EXCLUDED.percentage_error,
             measured_at = now()
           RETURNING id""",
        pursuit_id,
    )
    return len(rows)


async def _measure_all_pricing_predictions(pool: asyncpg.Pool) -> int:
    """Backfill accuracy rows for every simulation whose tender now has an award."""
    rows = await pool.fetch(
        """WITH actual AS (
               SELECT DISTINCT ON (p.id) p.id AS pursuit_id, w.award_value::numeric AS award_value
               FROM pursuits p
               JOIN awards w ON w.tender_id = p.tender_id
               WHERE w.award_value IS NOT NULL
               ORDER BY p.id, w.id
             )
           INSERT INTO pricing_prediction_results
             (simulation_id, pursuit_id, actual_award_value, predicted_price,
              absolute_error, percentage_error)
           SELECT s.id, s.pursuit_id, actual.award_value, s.proposed_price,
                  abs(s.proposed_price - actual.award_value),
                  (abs(s.proposed_price - actual.award_value) / nullif(actual.award_value, 0))::real
           FROM pursuit_simulations s
           JOIN actual ON actual.pursuit_id = s.pursuit_id
           ON CONFLICT (simulation_id) DO UPDATE SET
             actual_award_value = EXCLUDED.actual_award_value,
             predicted_price = EXCLUDED.predicted_price,
             absolute_error = EXCLUDED.absolute_error,
             percentage_error = EXCLUDED.percentage_error,
             measured_at = now()
           RETURNING id"""
    )
    return len(rows)


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
    measured_predictions = 0
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO outcomes (pursuit_id, result, submitted_value, award_value,
                                     competitor_count, notes)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT (pursuit_id) DO UPDATE SET result=EXCLUDED.result,
                 submitted_value=EXCLUDED.submitted_value, notes=EXCLUDED.notes,
                 award_value=EXCLUDED.award_value, competitor_count=EXCLUDED.competitor_count,
                 logged_at=now()""",
            pid, body.result, body.submitted_value, award_value, competitor_count, body.notes)
        if award_value:
            measured_predictions = await _measure_pricing_predictions(conn, pid)
        await conn.execute(
            "UPDATE pursuits SET stage=$2, updated_at=now() WHERE id=$1", pid, body.result)
        await conn.execute(
            """INSERT INTO ingest_events (event_type, entity_type, entity_id, data)
               VALUES ('outcome.logged', 'pursuit', $1, '{}')""", pid)
    return {"ok": True, "award_value": float(award_value) if award_value else None,
            "competitor_count": competitor_count,
            "measured_pricing_predictions": measured_predictions}


@app.get("/api/lanes")
async def lanes():
    """Ops: last run per ingestion lane + health verdict.

    A lane needs attention when its latest run failed or when it has not run
    within its expected cadence. Keeping ``stale`` as age-only preserves the
    old field while ``status`` and ``needs_attention`` make failures explicit.
    """
    rows = await app.state.pool.fetch(
        """SELECT DISTINCT ON (connector) connector, started_at, finished_at, ok, error
           FROM ingest_runs ORDER BY connector, started_at DESC""")
    expected_minutes = {
        "etimad.listing": 15,
        "etimad.reconcile": 26 * 60,
        "etimad.awards_harvest": 7 * 60,
    }
    out = []
    for r in rows:
        age_min = (
            r["started_at"]
            and await app.state.pool.fetchval(
                "SELECT extract(epoch FROM now()-$1)/60", r["started_at"]
            )
        )
        limit = expected_minutes.get(r["connector"])
        stale = bool(limit and age_min and age_min > limit)
        failed = r["ok"] is False
        status = "failed" if failed else ("stale" if stale else "healthy")
        out.append({
            "connector": r["connector"],
            "last_run": r["started_at"],
            "ok": r["ok"],
            "age_minutes": round(age_min) if age_min is not None else None,
            "stale": stale,
            "status": status,
            "needs_attention": failed or stale,
            "error": r["error"] if failed else None,
        })
    return out


@app.get("/api/market/price-position")
async def price_position():
    """Market insight from the harvested corpus: how often does the lowest
    technically-compliant bid win? Computed per multi-bidder awarded tender."""
    row = await app.state.pool.fetchrow(
        """WITH ranked AS (
             SELECT o.tender_id, o.is_winner,
                    rank() OVER (PARTITION BY o.tender_id ORDER BY o.offer_value) AS price_rank,
                    count(*) OVER (PARTITION BY o.tender_id) AS n_compliant
             FROM offers o
             WHERE o.technical_pass AND o.offer_value IS NOT NULL
           )
           SELECT count(*) FILTER (WHERE is_winner)                             AS awards_n,
                  count(*) FILTER (WHERE is_winner AND price_rank = 1)          AS lowest_won,
                  round(avg(price_rank) FILTER (WHERE is_winner), 2)            AS avg_winner_rank
           FROM ranked WHERE n_compliant >= 2""")
    d = dict(row)
    d["lowest_wins_pct"] = round(100 * d["lowest_won"] / d["awards_n"], 1) if d["awards_n"] else None
    return d


@app.post("/api/follows/{tender_id}")
async def follow(tender_id: int):
    n = await app.state.pool.execute(
        "INSERT INTO follows (tender_id) VALUES ($1) ON CONFLICT DO NOTHING", tender_id)
    return {"following": True, "created": n.endswith("1")}


@app.delete("/api/follows/{tender_id}")
async def unfollow(tender_id: int):
    await app.state.pool.execute("DELETE FROM follows WHERE tender_id=$1", tender_id)
    return {"following": False}


@app.get("/api/tenders.csv")
async def tenders_csv(
    q: str | None = None, agency_id: int | None = None, activity_id: int | None = None,
    awarded: bool | None = None, open_only: bool = False, source: str | None = None,
):
    """M2-5: Excel-ready export (UTF-8 BOM so Arabic opens correctly)."""
    import csv
    import io as _io

    from fastapi.responses import Response

    data = await tenders(q=q, agency_id=agency_id, activity_id=activity_id,
                         awarded=awarded, open_only=open_only, source=source,
                         limit=200, offset=0)
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["المرجع", "المنافسة", "الجهة", "النشاط", "المصدر", "الحالة",
                "آخر تقديم", "تاريخ النشر", "مُرسّاة"])
    for t in data["items"]:
        w.writerow([
            t.get("reference_number"), t.get("name"), t.get("agency"), t.get("activity"),
            "اعتماد" if t.get("source") == "etimad" else "فرصة",
            "مفتوحة" if (t.get("remaining_s") or 0) > 0 else "منتهية",
            str(t.get("last_offer_date") or "")[:16], str(t.get("published_at") or "")[:10],
            "نعم" if t.get("has_award") else "لا",
        ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="thaqip-tenders.csv"'},
    )


@app.get("/api/calibration")
async def calibration():
    """M5-4: prediction-vs-outcome pairs — the calibration dataset status."""
    pool: asyncpg.Pool = app.state.pool
    row = await pool.fetchrow(
        """SELECT count(pr.id)                                       AS predictions,
                  count(o.id)                                        AS resolved,
                  round(avg(pr.value) FILTER (WHERE o.id IS NOT NULL), 4)      AS avg_predicted,
                  round(avg((o.result='won')::int::numeric)
                        FILTER (WHERE o.id IS NOT NULL), 4)          AS actual_win_rate
           FROM predictions pr
           LEFT JOIN outcomes o ON o.pursuit_id = pr.pursuit_id""")
    pairs = await pool.fetch(
        """SELECT pr.pursuit_id, pr.value AS predicted, o.result,
                  left(t.name, 50) AS name
           FROM predictions pr
           JOIN outcomes o ON o.pursuit_id = pr.pursuit_id
           JOIN pursuits p ON p.id = pr.pursuit_id
           JOIN tenders t ON t.id = p.tender_id
           ORDER BY o.logged_at DESC LIMIT 20""")
    return {**dict(row), "recent_pairs": [dict(r) for r in pairs]}


@app.get("/api/pricing/accuracy")
async def pricing_accuracy():
    """M5: measured latest price prediction per pursuit versus announced awards."""
    pool: asyncpg.Pool = app.state.pool
    refreshed = await _measure_all_pricing_predictions(pool)
    row = await pool.fetchrow(
        """WITH latest AS (
               SELECT DISTINCT ON (r.pursuit_id) r.*
               FROM pricing_prediction_results r
               JOIN pursuit_simulations s ON s.id = r.simulation_id
               ORDER BY r.pursuit_id, s.created_at DESC, r.id DESC
             )
           SELECT count(*)::int AS measured,
                  round(avg(percentage_error)::numeric, 4)::float AS mape_all,
                  round(avg(percentage_error) FILTER (WHERE measured_at >= now() - interval '30 days')::numeric, 4)::float AS mape_30d,
                  round(avg(percentage_error) FILTER (WHERE measured_at >= now() - interval '90 days')::numeric, 4)::float AS mape_90d,
                  count(*) FILTER (WHERE measured_at >= now() - interval '30 days')::int AS sample_30d,
                  count(*) FILTER (WHERE measured_at >= now() - interval '90 days')::int AS sample_90d,
                  round(100.0 * count(*) FILTER (WHERE percentage_error <= 0.10) / nullif(count(*),0), 1)::float AS within_10_pct,
                  round(100.0 * count(*) FILTER (WHERE percentage_error <= 0.20) / nullif(count(*),0), 1)::float AS within_20_pct,
                  round(100.0 * count(*) FILTER (WHERE percentage_error <= 0.30) / nullif(count(*),0), 1)::float AS within_30_pct
           FROM latest"""
    )
    total_measured = await pool.fetchval("SELECT count(*) FROM pricing_prediction_results")
    recent = await pool.fetch(
        """WITH latest AS (
               SELECT DISTINCT ON (r.pursuit_id) r.*
               FROM pricing_prediction_results r
               JOIN pursuit_simulations s ON s.id = r.simulation_id
               ORDER BY r.pursuit_id, s.created_at DESC, r.id DESC
             )
           SELECT r.pursuit_id, left(t.name, 70) AS name,
                  r.predicted_price::float AS predicted_price,
                  r.actual_award_value::float AS actual_award_value,
                  r.absolute_error::float AS absolute_error,
                  round((r.percentage_error * 100)::numeric, 1)::float AS error_pct,
                  s.win_pct::float AS win_pct,
                  s.basis,
                  r.measured_at
           FROM latest r
           JOIN pursuit_simulations s ON s.id = r.simulation_id
           JOIN pursuits p ON p.id = r.pursuit_id
           JOIN tenders t ON t.id = p.tender_id
           ORDER BY r.measured_at DESC LIMIT 12"""
    )
    out = dict(row)
    out["total_measured_simulations"] = total_measured
    out["refreshed"] = refreshed
    out["status"] = "measured" if out["measured"] else "awaiting_awards"
    out["confidence"] = "high" if out["sample_90d"] >= 50 else "medium" if out["sample_90d"] >= 10 else "low"
    out["recent"] = [dict(r) for r in recent]
    return out


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
                   EXISTS (SELECT 1 FROM follows f WHERE f.tender_id = t.id) AS followed,
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

@app.get("/api/pursuits/{pid}/export/compliance")
async def export_compliance(pid: int):
    """M3-1: Export pursuit compliance matrix as CSV (Arabic UTF-8 with BOM)."""
    import csv
    import io

    from fastapi.responses import StreamingResponse

    pool: asyncpg.Pool = app.state.pool
    p = await pool.fetchrow(
        """SELECT p.*, t.name, t.reference_number
           FROM pursuits p JOIN tenders t ON t.id = p.tender_id
           WHERE p.id=$1""", pid)
    if p is None:
        raise HTTPException(404, "pursuit not found")

    items = await pool.fetch(
        """SELECT sort_order, requirement, category, source_ref, status, origin, confidence
           FROM compliance_items WHERE pursuit_id=$1
           ORDER BY sort_order, id""", pid)

    stream = io.StringIO()
    # Write UTF-8 BOM so Excel opens Arabic correctly
    stream.write("\ufeff")
    writer = csv.writer(stream)
    writer.writerow(["م", "المتطلب", "التصنيف", "المرجع النظامي / الفني", "الحالة", "المصدر", "نسبة الثقة"])
    for i, it in enumerate(items, 1):
        writer.writerow([
            i,
            it["requirement"],
            it["category"],
            it["source_ref"],
            it["status"],
            it["origin"],
            f"{it['confidence'] * 100:.0f}%" if it["confidence"] is not None else "",
        ])

    stream.seek(0)
    filename = f"compliance_pursuit_{pid}_{p['reference_number']}.csv"
    return StreamingResponse(
        io.BytesIO(stream.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

class PriceSimulationIn(BaseModel):
    proposed_price: float
    target_margin_pct: float | None = None


def _score_band(score: int) -> str:
    if score >= 75:
        return "go"
    if score >= 50:
        return "review"
    return "no_bid"


def _score_confidence(sample_count: int, has_recent_simulation: bool) -> str:
    if sample_count >= 20 and has_recent_simulation:
        return "high"
    if sample_count >= 5 or has_recent_simulation:
        return "medium"
    return "low"


def _war_room_decision(tender: dict, compliance: list[dict], market: dict) -> dict:
    actionable = [c for c in compliance if c.get("status") != "n_a"]
    met = [c for c in actionable if c.get("status") == "met"]
    readiness = round(100 * len(met) / len(actionable)) if actionable else 0

    remaining_s = tender.get("remaining_s") or 0
    days_left = remaining_s / 86400 if remaining_s else 0
    live_bidders = (tender.get("submitted_bids_count") or 0) + (tender.get("external_bids_count") or 0)
    award_samples = int(market.get("award_samples") or 0)
    median_award = market.get("median_award")
    last_win_pct = market.get("last_win_pct")
    has_recent_simulation = last_win_pct is not None

    decision_score = 42
    decision_score += min(28, round(readiness * 0.28))
    if days_left >= 5:
        decision_score += 10
    elif days_left >= 2:
        decision_score += 5
    elif days_left > 0:
        decision_score -= 8
    else:
        decision_score -= 25
    if award_samples >= 20:
        decision_score += 10
    elif award_samples >= 5:
        decision_score += 5
    else:
        decision_score -= 4
    if last_win_pct is not None:
        if last_win_pct >= 35:
            decision_score += 10
        elif last_win_pct < 15:
            decision_score -= 8
    if live_bidders >= 10:
        decision_score -= 10
    elif live_bidders >= 5:
        decision_score -= 5
    decision_score = max(0, min(100, decision_score))

    risk_flags = []
    next_actions = []
    missing_docs = [c for c in actionable if c.get("category") in ("document", "guarantee", "qualification") and c.get("status") == "missing"]
    open_deadlines = [c for c in actionable if c.get("category") == "deadline" and c.get("status") == "missing"]

    if days_left <= 0:
        risk_flags.append({"level": "critical", "label": "انتهى موعد التقديم", "reason": "لا يظهر وقت متبقٍ لتقديم العرض."})
    elif days_left < 2:
        risk_flags.append({"level": "high", "label": "وقت ضيق", "reason": "أقل من يومين على الإغلاق."})
        next_actions.append("تثبيت قرار الدخول اليوم وتجميد نطاق السعر.")
    if missing_docs:
        risk_flags.append({"level": "high", "label": "نواقص امتثال", "reason": f"{len(missing_docs)} متطلبات نظامية أو تأهيلية غير مكتملة."})
        next_actions.append("إغلاق نواقص السجل التجاري والزكاة والتأمينات والضمان قبل التسعير النهائي.")
    if open_deadlines:
        next_actions.append("تأكيد مواعيد الاستفسارات والتقديم داخل ملف العرض.")
    if live_bidders >= 5:
        risk_flags.append({"level": "medium", "label": "منافسة مرتفعة", "reason": f"يوجد {live_bidders} عروض/اهتمامات مرصودة."})
    if award_samples < 5:
        risk_flags.append({"level": "medium", "label": "عينة تسعير ضعيفة", "reason": "الترسيات المشابهة غير كافية لثقة عالية."})
        next_actions.append("تشغيل حصاد ترسيات أعمق للنشاط أو الجهة قبل اعتماد السعر.")
    if last_win_pct is None:
        next_actions.append("شغّل محاكي التسعير لحفظ توقع سعري قابل للقياس لاحقاً.")
    elif last_win_pct < 15:
        next_actions.append("راجع السعر المقترح؛ آخر محاكاة تعطي احتمال فوز منخفضاً.")
    if median_award:
        next_actions.append(f"استخدم وسيط الترسيات المشابهة كنقطة مرجعية: {median_award:,.0f} ر.س.")
    if not next_actions:
        next_actions.append("الملف جاهز للمراجعة التجارية النهائية وتثبيت السعر.")

    return {
        "decision_score": decision_score,
        "decision_band": _score_band(decision_score),
        "readiness_score": readiness,
        "confidence": _score_confidence(award_samples, has_recent_simulation),
        "market": market,
        "risk_flags": risk_flags[:5],
        "next_actions": next_actions[:5],
    }


@app.post("/api/pursuits/{pid}/simulate-price")
async def simulate_price(pid: int, body: PriceSimulationIn):
    """M5-2, M5-3: Dynamic Win-Probability & Pricing Intelligence Simulator."""
    if body.proposed_price <= 0:
        raise HTTPException(422, "Proposed price must be greater than zero")

    pool: asyncpg.Pool = app.state.pool
    p = await pool.fetchrow(
        """SELECT p.id, t.*
           FROM pursuits p
           JOIN tenders t ON t.id = p.tender_id
           WHERE p.id = $1""", pid
    )
    if p is None:
        raise HTTPException(404, "pursuit not found")

    tender = dict(p)
    activity_id = tender.get("activity_id")
    tender_id = tender.get("id")

    async with pool.acquire() as conn:
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

        basis = "activity_history"
        if tender.get("source") == "forsah" and live_bidders > 0:
            basis = "live_bidders"
            effective_bidders = float(live_bidders)
        else:
            effective_bidders = max(float(median_bidders), 1.0)

        baseline_p = 1.0 / (effective_bidders + 1.0)
        import math
        if median_award and median_award > 0:
            ratio = body.proposed_price / median_award
            win_prob = 1.0 / (1.0 + math.exp(4.0 * (ratio - 0.95)))
            win_prob = max(0.02, min(0.95, win_prob))
        else:
            basis = "density_fallback"
            win_prob = baseline_p

        win_prob_pct = round(win_prob * 100.0, 1)
        gtpl_abnormally_low = bool(median_award and body.proposed_price < (0.70 * median_award))
        expected_value = round(body.proposed_price * (win_prob_pct / 100.0), 2)

        if not median_award or not p25_award or not p75_award:
            zone = "sweet_spot"
        elif body.proposed_price < p25_award:
            zone = "aggressive"
        elif body.proposed_price <= median_award:
            zone = "sweet_spot"
        elif body.proposed_price <= p75_award:
            zone = "conservative"
        else:
            zone = "uncompetitive"

        benchmarks = {
            "sample_count": sample_count,
            "min_award": min_award,
            "p25_award": p25_award,
            "median_award": median_award,
            "p75_award": p75_award,
            "max_award": max_award,
            "median_bidders": median_bidders,
        }

        # Snapshot into pursuit_simulations
        import json
        await conn.execute(
            """INSERT INTO pursuit_simulations (pursuit_id, proposed_price, win_pct, expected_value, basis, metadata)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb)""",
            pid, body.proposed_price, win_prob_pct, expected_value, basis, json.dumps(benchmarks)
        )

        return {
            "proposed_price": body.proposed_price,
            "win_probability_pct": win_prob_pct,
            "expected_value": expected_value,
            "competitive_zone": zone,
            "gtpl_abnormally_low_flag": gtpl_abnormally_low,
            "basis": basis,
            "benchmarks": benchmarks,
            "recommendations": {
                "optimal_price": round(median_award * 0.92, 2) if median_award else round(body.proposed_price * 0.95, 2),
                "safe_margin_floor": round(median_award * 0.72, 2) if median_award else round(body.proposed_price * 0.75, 2),
            },
        }

@app.get("/api/vendors/{vid}/export")
async def export_vendor_dossier(vid: int):
    """M8-1: Export competitor bidding dossier as CSV (Arabic UTF-8 with BOM)."""
    import csv
    import io

    from fastapi.responses import StreamingResponse

    pool: asyncpg.Pool = app.state.pool
    v = await pool.fetchrow("SELECT * FROM vendors WHERE id=$1", vid)
    if v is None:
        raise HTTPException(404, "vendor not found")

    history = await pool.fetch("""
        SELECT t.id AS tender_id, t.name AS tender_name, t.reference_number,
               coalesce(a.canonical_name, t.agency_name_raw) AS agency,
               t.activity_name_raw AS activity,
               o.offer_value, o.is_winner, o.technical_pass
        FROM offers o
        JOIN tenders t ON t.id = o.tender_id
        LEFT JOIN agencies a ON a.id = t.agency_id
        WHERE o.vendor_id=$1 ORDER BY o.id DESC LIMIT 500""", vid)

    stream = io.StringIO()
    stream.write("\ufeff")
    writer = csv.writer(stream)
    writer.writerow(["المنافسة", "الرقم المرجعي", "الجهة الحكومية", "النشاط", "قيمة العرض (ر.س)", "مطابق فنياً", "فاز بالمنافسة"])
    for h in history:
        writer.writerow([
            h["tender_name"],
            h["reference_number"] or "",
            h["agency"] or "",
            h["activity"] or "",
            f"{float(h['offer_value']):,.2f}" if h["offer_value"] is not None else "",
            "نعم" if h["technical_pass"] else ("لا" if h["technical_pass"] is False else "—"),
            "نعم" if h["is_winner"] else "لا",
        ])

    stream.seek(0)
    filename = f"competitor_dossier_{vid}.csv"
    return StreamingResponse(
        io.BytesIO(stream.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
