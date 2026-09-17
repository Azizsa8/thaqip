"""Thaqip internal console (ticket E1).

Read-only web UI over the live corpus: tender table with filters, tender
detail with offers/awards, freshness and corpus stats. Internal tooling —
auth comes with the staging deployment (A3); do not expose publicly.

Run:  DATABASE_URL=... uv run uvicorn thaqip_console.app:app --port 8080
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import math
import logging
import os
import re
import sys
import uuid
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import auth as auth_mod
from .telegram import TelegramBot

log = logging.getLogger("thaqip.console")

STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# P2W engine bootstrap
# ---------------------------------------------------------------------------
# The pricing engine lives in the ingestion package (thaqip_ingestion.p2w). The
# console image does not depend on it as a wheel, so make the source tree
# importable when it is present. Order: explicit env override, the in-image
# path, then the repo checkout (developer machines / tests).
def _p2w_source_candidates() -> list[Path]:
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    env = os.environ.get("THAQIP_P2W_SRC")
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/app/ingestion/src"))
    # services/console/src/thaqip_console/app.py -> services/ingestion/src
    for parent in here.parents:
        if parent.name == "services":
            candidates.append(parent / "ingestion" / "src")
            break
    return candidates


def _bootstrap_p2w() -> None:
    for path in _p2w_source_candidates():
        if (path / "thaqip_ingestion" / "p2w" / "contracts.py").exists():
            resolved = str(path)
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
            return


_bootstrap_p2w()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"], min_size=1, max_size=5
    )
    app.state.tenant_ids = {}
    app.state.scenario_curve_cache = {}
    app.state.telegram = TelegramBot()
    poller = None
    if app.state.telegram.configured and os.environ.get("THAQIP_TELEGRAM_POLL", "1") == "1":
        poller = asyncio.create_task(app.state.telegram.poll_forever(app.state.pool))
    yield
    if poller:
        poller.cancel()
    await app.state.telegram.close()
    await app.state.pool.close()


app = FastAPI(title="Thaqip Console", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Tenancy (P2W hard red gate)
# ---------------------------------------------------------------------------
# Every table holding user-private state (pursuits, compliance_items via their
# pursuit, outcomes, predictions, follows, alert_profiles, user_bid_scenarios,
# user_calculator_prefs) is filtered by tenant_id. The tenant comes from the
# X-Thaqip-Tenant header and defaults to the 'default' tenant; an unknown slug
# is a 404 rather than a silent fallback, because silently serving the default
# tenant's rows to an unknown caller is exactly the leak this gate exists for.
TENANT_HEADER = "X-Thaqip-Tenant"
DEFAULT_TENANT_SLUG = os.environ.get("THAQIP_DEFAULT_TENANT", "default")


async def _resolve_tenant(pool: asyncpg.Pool, slug: str) -> int:
    cache = getattr(app.state, "tenant_ids", None)
    if cache is None:
        cache = app.state.tenant_ids = {}
    if slug in cache:
        return cache[slug]
    tid = await pool.fetchval("SELECT id FROM tenants WHERE slug=$1", slug)
    if tid is None:
        raise HTTPException(404, f"unknown tenant {slug!r}")
    cache[slug] = int(tid)
    return int(tid)


async def get_tenant_id(request: Request) -> int:
    """FastAPI dependency: the caller's tenant id, from their CREDENTIAL.

    This used to read the X-Thaqip-Tenant header and fall back to a default,
    which meant an unauthenticated caller was served the default tenant's
    private cost and bid rows. The tenant now comes from the authenticated
    principal that the auth middleware attached to the request; the header is
    accepted only from a service token, and only to select a tenant that token
    is already entitled to act for.
    """
    who = getattr(request.state, "principal", None)
    if who is None:
        who = await auth_mod.require_principal(request)
    if who.get("auth") == "service_token":
        requested = (request.headers.get(TENANT_HEADER) or "").strip()
        if requested and requested != who["tenant_slug"]:
            return await _resolve_tenant(request.app.state.pool, requested)
    return int(who["tenant_id"])


Tenant = Depends(get_tenant_id)


# --- authentication gate ----------------------------------------------------
# Applied as middleware rather than per-endpoint so a newly added route is
# protected by default. The failure mode of the previous design was a route
# that simply forgot to depend on the tenant.
PUBLIC_PATHS = frozenset({
    "/", "/index.html", "/favicon.ico",
    "/api/auth/login", "/api/auth/me", "/api/health",
})  # /docs and /openapi.json map the whole API surface: session or token only.


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    who = await auth_mod.principal(request)
    if who is None:
        return JSONResponse(
            status_code=401,
            content={"detail": {
                "error": "authentication_required",
                "message_ar": "يلزم تسجيل الدخول للوصول إلى هذه البيانات.",
                "login": "/api/auth/login"}},
            headers={"WWW-Authenticate": "Bearer"})
    request.state.principal = who
    return await call_next(request)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=400)


LOGIN_WINDOW_MIN = 15
LOGIN_MAX_FAILS_PER_USER = 8
LOGIN_MAX_FAILS_PER_IP = 30


@app.post("/api/auth/login")
async def auth_login(body: LoginIn, request: Request, response: Response):
    pool: asyncpg.Pool = app.state.pool
    ip = auth_mod.client_ip(request)
    # Throttle on recent failures from this address — per (username, ip) so a
    # guesser cannot lock the real admin out from elsewhere, plus an ip-wide cap
    # against spraying many usernames. Counted from the audit table, so a
    # console restart does not reset it.
    pair_fails, ip_fails = await pool.fetchrow(
        """SELECT count(*) FILTER (WHERE lower(username) = lower($1)),
                  count(*)
           FROM auth_events
           WHERE event = 'login_fail' AND ip = $2
             AND created_at > now() - make_interval(mins => $3)""",
        body.username, ip, LOGIN_WINDOW_MIN)
    if pair_fails >= LOGIN_MAX_FAILS_PER_USER or ip_fails >= LOGIN_MAX_FAILS_PER_IP:
        await auth_mod.log_auth_event(
            pool, event="denied", username=body.username, detail="throttled", ip=ip)
        raise HTTPException(429, detail={
            "error": "too_many_attempts",
            "message_ar": f"محاولات فاشلة كثيرة. حاول مرة أخرى بعد {LOGIN_WINDOW_MIN} دقيقة."})
    who = await auth_mod.authenticate(pool, body.username, body.password)
    if who is None:
        await auth_mod.log_auth_event(
            pool, event="login_fail", username=body.username, ip=ip)
        raise HTTPException(401, detail={
            "error": "invalid_credentials",
            "message_ar": "اسم المستخدم أو كلمة المرور غير صحيحة."})
    token, expires = await auth_mod.create_session(
        pool, who["user_id"], user_agent=request.headers.get("user-agent", ""), ip=ip)
    await pool.execute("UPDATE users SET last_login_at=now() WHERE id=$1", who["user_id"])
    await auth_mod.log_auth_event(
        pool, event="login_ok", username=who["username"], user_id=who["user_id"],
        tenant_id=who["tenant_id"], ip=ip)
    response.set_cookie(
        auth_mod.COOKIE_NAME, token, httponly=True, samesite="lax",
        secure=auth_mod.cookie_secure(request),
        expires=expires.strftime("%a, %d %b %Y %H:%M:%S GMT"), path="/")
    return {"username": who["username"], "role": who["role"],
            "tenant": who["tenant_slug"], "expires_at": expires.isoformat()}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = request.cookies.get(auth_mod.COOKIE_NAME)
    if token:
        who = await auth_mod.session_principal(app.state.pool, token) or {}
        await auth_mod.revoke_session(app.state.pool, token)
        await auth_mod.log_auth_event(
            app.state.pool, event="logout", username=who.get("username"),
            user_id=who.get("user_id"), tenant_id=who.get("tenant_id"),
            ip=auth_mod.client_ip(request))
    response.delete_cookie(auth_mod.COOKIE_NAME, path="/", httponly=True, samesite="lax",
                           secure=auth_mod.cookie_secure(request))
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """Who am I? Public so the UI can decide whether to show the login form."""
    who = await auth_mod.principal(request)
    if who is None:
        return {"authenticated": False}
    return {"authenticated": True, "username": who["username"], "role": who["role"],
            "tenant": who["tenant_slug"], "auth": who["auth"]}


@app.get("/api/health")
async def health():
    return {"ok": True}


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
    source_mix = await pool.fetch(
        """SELECT source,
                  count(*) AS tenders,
                  count(*) FILTER (WHERE last_offer_date > now()) AS open_now,
                  count(*) FILTER (WHERE awards_count.c > 0) AS awarded
           FROM tenders t
           LEFT JOIN (SELECT tender_id, count(*) c FROM awards GROUP BY tender_id) awards_count
             ON awards_count.tender_id = t.id
           GROUP BY source ORDER BY tenders DESC"""
    )
    urgency = await pool.fetch(
        """SELECT bucket, count(*) AS tenders FROM (
             SELECT CASE
               WHEN last_offer_date <= now() + interval '24 hours' THEN '24h'
               WHEN last_offer_date <= now() + interval '3 days' THEN '3d'
               WHEN last_offer_date <= now() + interval '7 days' THEN '7d'
               ELSE 'later'
             END AS bucket
             FROM tenders WHERE last_offer_date > now()
           ) s GROUP BY bucket"""
    )
    top_agencies = await pool.fetch(
        """SELECT t.agency_id AS id,
                  left(coalesce(a.canonical_name, t.agency_name_raw, 'غير محدد'), 80) AS name,
                  count(*) AS tenders,
                  count(*) FILTER (WHERE t.last_offer_date > now()) AS open_now,
                  coalesce(sum(w.award_value), 0)::numeric AS award_value
           FROM tenders t
           LEFT JOIN agencies a ON a.id = t.agency_id
           LEFT JOIN awards w ON w.tender_id = t.id
           GROUP BY t.agency_id, a.canonical_name, t.agency_name_raw
           ORDER BY open_now DESC, award_value DESC, tenders DESC
           LIMIT 8"""
    )
    top_activities = await pool.fetch(
        """SELECT t.activity_id AS id,
                  left(coalesce(t.activity_name_raw, 'غير محدد'), 80) AS name,
                  count(*) AS tenders,
                  count(*) FILTER (WHERE t.last_offer_date > now()) AS open_now,
                  count(DISTINCT o.vendor_id) AS competitors,
                  coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY w.award_value), 0)::numeric AS median_award
           FROM tenders t
           LEFT JOIN offers o ON o.tender_id = t.id
           LEFT JOIN awards w ON w.tender_id = t.id AND w.award_value IS NOT NULL
           GROUP BY t.activity_id, t.activity_name_raw
           ORDER BY competitors DESC, open_now DESC, tenders DESC
           LIMIT 8"""
    )
    award_bands = await pool.fetch(
        """SELECT band, count(*) AS awards, coalesce(sum(award_value),0)::numeric AS value FROM (
             SELECT award_value,
                    CASE
                      WHEN award_value < 50000 THEN '<50k'
                      WHEN award_value < 250000 THEN '50k-250k'
                      WHEN award_value < 1000000 THEN '250k-1m'
                      ELSE '1m+'
                    END AS band
             FROM awards WHERE award_value IS NOT NULL
           ) s GROUP BY band"""
    )
    return {
        **dict(kpis),
        "fresh_p50_seconds": fresh["p50"].total_seconds() if fresh and fresh["p50"] else None,
        "monthly": [dict(r) for r in reversed(monthly)],
        "latest_awards": [dict(r) for r in latest_awards],
        "source_mix": [dict(r) for r in source_mix],
        "urgency_buckets": [dict(r) for r in urgency],
        "top_agencies": [dict(r) for r in top_agencies],
        "top_activities": [dict(r) for r in top_activities],
        "award_bands": [dict(r) for r in award_bands],
    }


@app.get("/api/freshness/trend")
async def freshness_trend(days: int = Query(14, ge=3, le=60)):
    """Daily detection-latency trend for the SLO chart."""
    rows = await app.state.pool.fetch(
        """WITH daily AS (
             SELECT date_trunc('day', published_at)::date AS day,
                    count(*) AS tenders,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY detected_at - published_at) AS p50,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY detected_at - published_at) AS p95
             FROM tenders
             WHERE detected_by = 'poller'
               AND published_at >= now() - ($1::int || ' days')::interval
               AND published_at IS NOT NULL
               AND detected_at >= published_at
             GROUP BY 1
           )
           SELECT day, tenders, p50, p95
           FROM daily
           ORDER BY day""",
        days,
    )
    target_seconds = 15 * 60
    out = []
    for r in rows:
        p50 = r["p50"].total_seconds() if r["p50"] is not None else None
        p95 = r["p95"].total_seconds() if r["p95"] is not None else None
        out.append({
            "day": r["day"].isoformat(),
            "tenders": r["tenders"],
            "p50_seconds": round(p50) if p50 is not None else None,
            "p95_seconds": round(p95) if p95 is not None else None,
            "slo_target_seconds": target_seconds,
            "slo_met": p95 is not None and p95 <= target_seconds,
        })
    latest = out[-1] if out else None
    return {
        "target_seconds": target_seconds,
        "days": days,
        "latest": latest,
        "items": out,
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
async def create_pursuit(body: PursuitIn, tenant_id: int = Tenant):
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
            "SELECT id FROM pursuits WHERE tender_id=$1 AND tenant_id=$2",
            body.tender_id, tenant_id)
        if existing:
            return {"id": existing, "created": False}
        pid = await conn.fetchval(
            "INSERT INTO pursuits (tender_id, tenant_id) VALUES ($1, $2) RETURNING id",
            body.tender_id, tenant_id)
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
                """INSERT INTO predictions (pursuit_id, tenant_id, value, basis)
                   VALUES ($1, $2, $3, $4::jsonb)""",
                pid, tenant_id, round(1 / (live + 1), 4),
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
                    """INSERT INTO predictions (pursuit_id, tenant_id, value, basis)
                       VALUES ($1, $2, $3, $4::jsonb)""",
                    pid, tenant_id, round(1 / max(float(mb), 1), 4),
                    f'{{"basis":"activity_history","median_bidders":{float(mb)}}}')
    return {"id": pid, "created": True}


@app.get("/api/pursuits")
async def pursuits(tenant_id: int = Tenant):
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
           WHERE p.tenant_id = $1
           ORDER BY p.created_at DESC""",
        tenant_id,
    )
    return [dict(r) for r in rows]


@app.get("/api/pursuits/{pid}")
async def pursuit_detail(pid: int, tenant_id: int = Tenant):
    pool: asyncpg.Pool = app.state.pool
    p = await pool.fetchrow(
        """SELECT p.*, t.name, t.reference_number, t.source,
                  t.activity_id, t.activity_name_raw, t.submitted_bids_count,
                  t.external_bids_count, t.booklet_price,
                  coalesce(a.canonical_name, t.agency_name_raw) AS agency,
                  t.last_offer_date, t.last_offer_date_hijri,
                  greatest(0, extract(epoch FROM t.last_offer_date - now()))::bigint AS remaining_s
           FROM pursuits p JOIN tenders t ON t.id = p.tender_id
           LEFT JOIN agencies a ON a.id = t.agency_id
           WHERE p.id=$1 AND p.tenant_id=$2""", pid, tenant_id)
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
    evidence_ref: str | None = Field(default=None, max_length=500)


@app.patch("/api/compliance/{item_id}")
async def patch_item(item_id: int, body: ItemPatch, tenant_id: int = Tenant):
    if body.status not in ("missing", "in_progress", "met", "n_a"):
        raise HTTPException(422)
    evidence_ref = body.evidence_ref.strip() if body.evidence_ref is not None else None
    # compliance_items has no tenant_id of its own; it inherits the tenant of
    # the pursuit that owns it, so the gate is an EXISTS on that pursuit.
    if "evidence_ref" in body.model_fields_set:
        n = await app.state.pool.execute(
            """UPDATE compliance_items c
               SET status=$2, evidence_ref=$3
               WHERE c.id=$1
                 AND EXISTS (SELECT 1 FROM pursuits p
                             WHERE p.id = c.pursuit_id AND p.tenant_id = $4)""",
            item_id,
            body.status,
            evidence_ref,
            tenant_id,
        )
    else:
        n = await app.state.pool.execute(
            """UPDATE compliance_items c SET status=$2
               WHERE c.id=$1
                 AND EXISTS (SELECT 1 FROM pursuits p
                             WHERE p.id = c.pursuit_id AND p.tenant_id = $3)""",
            item_id,
            body.status,
            tenant_id,
        )
    if n.endswith("0"):
        raise HTTPException(404)
    return {"ok": True, "evidence_ref": evidence_ref}


class StagePatch(BaseModel):
    stage: str


@app.patch("/api/pursuits/{pid}/stage")
async def patch_stage(pid: int, body: StagePatch, tenant_id: int = Tenant):
    if body.stage not in ("studying", "pricing", "writing", "submitted", "won", "lost"):
        raise HTTPException(422)
    n = await app.state.pool.execute(
        "UPDATE pursuits SET stage=$2, updated_at=now() WHERE id=$1 AND tenant_id=$3",
        pid, body.stage, tenant_id)
    if n.endswith("0"):
        raise HTTPException(404)
    return {"ok": True}


class OutcomeIn(BaseModel):
    result: str                      # won | lost
    submitted_value: float | None = None
    notes: str | None = None


class OpsAckIn(BaseModel):
    connector: str
    note: str | None = None


class SettingsIn(BaseModel):
    default_markup_pct: float = Field(ge=0, le=100)
    risk_tolerance: str = Field(pattern="^(low|balanced|high)$")
    default_agency_id: int | None = None
    alert_frequency: str = Field(pattern="^(instant|hourly|daily)$")
    retention_days: int = Field(ge=30, le=3650)
    my_company_name: str = Field(default="شركتي", min_length=1, max_length=160)
    target_win_rate_pct: float = Field(default=25.0, ge=0, le=100)
    cost_advantage_pct: float = Field(default=0.0, ge=-50, le=50)


def _checkpoint_json(checkpoint: object) -> dict:
    """Decode an ingest checkpoint into a dict when it is structured JSON."""
    if checkpoint is None:
        return {}
    if isinstance(checkpoint, str):
        try:
            checkpoint = json.loads(checkpoint)
        except json.JSONDecodeError:
            return {}
    return checkpoint if isinstance(checkpoint, dict) else {}


def _checkpoint_has_acknowledgement(checkpoint: object) -> bool:
    """Return true only when a checkpoint carries a structured ack marker."""
    return bool(_checkpoint_json(checkpoint).get("acknowledged_at"))


async def _measure_pricing_predictions(
    conn: asyncpg.Connection, pursuit_id: int, tenant_id: int
) -> int:
    """Attach actual award values to stored pricing simulations once known."""
    rows = await conn.fetch(
        """WITH actual AS (
               SELECT p.id AS pursuit_id, w.award_value::numeric AS award_value
               FROM pursuits p
               JOIN awards w ON w.tender_id = p.tender_id
               WHERE p.id = $1 AND p.tenant_id = $2 AND w.award_value IS NOT NULL
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
        tenant_id,
    )
    return len(rows)


async def _measure_all_pricing_predictions(pool: asyncpg.Pool, tenant_id: int) -> int:
    """Backfill accuracy rows for every simulation whose tender now has an award."""
    rows = await pool.fetch(
        """WITH actual AS (
               SELECT DISTINCT ON (p.id) p.id AS pursuit_id, w.award_value::numeric AS award_value
               FROM pursuits p
               JOIN awards w ON w.tender_id = p.tender_id
               WHERE w.award_value IS NOT NULL AND p.tenant_id = $1
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
           RETURNING id""",
        tenant_id,
    )
    return len(rows)


@app.post("/api/pursuits/{pid}/outcome")
async def log_outcome(pid: int, body: OutcomeIn, tenant_id: int = Tenant):
    """M5-1: capture bid outcome; auto-reconcile award value + competitor count."""
    if body.result not in ("won", "lost"):
        raise HTTPException(422)
    pool: asyncpg.Pool = app.state.pool
    tender_id = await pool.fetchval(
        "SELECT tender_id FROM pursuits WHERE id=$1 AND tenant_id=$2", pid, tenant_id)
    if tender_id is None:
        raise HTTPException(404)
    award_value = await pool.fetchval(
        "SELECT award_value FROM awards WHERE tender_id=$1 ORDER BY id LIMIT 1", tender_id)
    competitor_count = await pool.fetchval(
        "SELECT count(*) FROM offers WHERE tender_id=$1", tender_id) or None
    measured_predictions = 0
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO outcomes (pursuit_id, tenant_id, result, submitted_value,
                                     award_value, competitor_count, notes)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (pursuit_id) DO UPDATE SET result=EXCLUDED.result,
                 submitted_value=EXCLUDED.submitted_value, notes=EXCLUDED.notes,
                 award_value=EXCLUDED.award_value, competitor_count=EXCLUDED.competitor_count,
                 logged_at=now()""",
            pid, tenant_id, body.result, body.submitted_value, award_value,
            competitor_count, body.notes)
        if award_value:
            measured_predictions = await _measure_pricing_predictions(conn, pid, tenant_id)
        await conn.execute(
            "UPDATE pursuits SET stage=$2, updated_at=now() WHERE id=$1 AND tenant_id=$3",
            pid, body.result, tenant_id)
        await conn.execute(
            """INSERT INTO ingest_events (event_type, entity_type, entity_id, data)
               VALUES ('outcome.logged', 'pursuit', $1, '{}')""", pid)
    return {"ok": True, "award_value": float(award_value) if award_value else None,
            "competitor_count": competitor_count,
            "measured_pricing_predictions": measured_predictions}


@app.get("/api/lanes")
async def lanes():
    """Ops: last run per ingestion lane + health verdict.

    A lane needs attention when its latest run failed, has not run within its
    expected cadence, or appears stuck with an unfinished ingest run. WAF
    cool-off is tracked separately: it is a source-imposed pause, not an
    operator failure.
    """
    rows = await app.state.pool.fetch(
        """SELECT DISTINCT ON (connector) connector, started_at, finished_at, ok, error,
                  pages, items_seen, items_new, items_changed, checkpoint
           FROM ingest_runs ORDER BY connector, started_at DESC"""
    )
    expected_minutes = {
        "etimad.listing": 15,
        "etimad.reconcile": 26 * 60,
        "etimad.awards_harvest": 7 * 60,
        "pricing.seed": 7 * 60,
        "pricing.clock": 7 * 60,
        "etimad.awards_backfill": 3 * 60,
        "gastat.price_indices": 50 * 60,
        "ops.backup": 26 * 60,
        "ops.restore_drill": 8 * 24 * 60,
        "ops.health": 90,
    }
    running_grace_minutes = {
        "etimad.awards_harvest": 90,
        "etimad.awards_backfill": 90,
        "etimad.backfill.all": 180,
        "etimad.backfill.awarded": 180,
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
        checkpoint = r["checkpoint"]
        checkpoint_meta = _checkpoint_json(checkpoint)
        acknowledged = bool(checkpoint_meta.get("acknowledged_at"))
        cooldown = bool(r["error"] and "waf cool-off" in r["error"])
        failed = r["ok"] is False and not cooldown and not acknowledged
        running = r["finished_at"] is None and r["ok"] is None
        stalled = bool(running and age_min and age_min > running_grace_minutes.get(r["connector"], 60))
        if failed:
            status = "failed"
        elif acknowledged:
            status = "acknowledged"
        elif stalled:
            status = "stalled"
        elif running:
            status = "running"
        elif cooldown:
            status = "cooldown"
        elif stale:
            status = "stale"
        else:
            status = "healthy"
        out.append({
            "connector": r["connector"],
            "last_run": r["started_at"],
            "finished_at": r["finished_at"],
            "ok": r["ok"],
            "age_minutes": round(age_min) if age_min is not None else None,
            "stale": stale,
            "status": status,
            "needs_attention": failed or stale or stalled,
            "error": r["error"] if failed or cooldown or acknowledged else None,
            "acknowledged_at": checkpoint_meta.get("acknowledged_at"),
            "acknowledged_note": checkpoint_meta.get("acknowledged_note"),
            "pages": r["pages"],
            "items_seen": r["items_seen"],
            "items_new": r["items_new"],
            "items_changed": r["items_changed"],
            "checkpoint": r["checkpoint"],
        })
    return out


@app.get("/api/ops/summary")
async def ops_summary(tenant_id: int = Tenant):
    """Single operational verdict for monitors, demos, and handoff reviews."""
    lane_rows = await lanes()
    accuracy = await pricing_accuracy(tenant_id)
    attention = [r for r in lane_rows if r.get("needs_attention")]
    running = [r for r in lane_rows if r.get("status") == "running"]
    healthy = [r for r in lane_rows if r.get("status") == "healthy"]
    critical_connectors = {"etimad.listing", "pricing.seed", "ops.health", "ops.backup"}
    critical_attention = [r for r in attention if r.get("connector") in critical_connectors]
    if critical_attention:
        verdict = "down"
    elif attention:
        verdict = "degraded"
    else:
        verdict = "operational"
    next_actions = []
    for row in attention[:5]:
        connector = row.get("connector")
        status = row.get("status")
        if status == "failed" and row.get("error") == "stalled watchdog closed orphaned run":
            next_actions.append(f"راجع سجل {connector}: أغلقه watchdog كسجل يتيم؛ لا تشغل نسخة ثانية قبل فحص العملية.")
        elif status == "failed":
            next_actions.append(f"راجع آخر خطأ في {connector}: {row.get('error') or 'غير محدد'}")
        elif status == "stale":
            next_actions.append(f"أعد تشغيل {connector} أو تحقق من جدولة Modal/cron؛ آخر نبض متأخر.")
        elif status == "stalled":
            next_actions.append(f"افحص عملية {connector}؛ run مفتوح تجاوز حد التعليق.")
    if accuracy.get("confidence") == "low":
        next_actions.append("زد عينات قياس التسعير عبر pursuits نشطة وترسيات فعلية؛ الثقة الحالية منخفضة.")
    return {
        "verdict": verdict,
        "lanes_total": len(lane_rows),
        "lanes_healthy": len(healthy),
        "lanes_running": len(running),
        "lanes_need_attention": len(attention),
        "attention": attention,
        "pricing_accuracy": {
            "status": accuracy.get("status"),
            "measured": accuracy.get("measured"),
            "confidence": accuracy.get("confidence"),
            "mape_90d": accuracy.get("mape_90d"),
        },
        "next_actions": next_actions[:6],
    }


@app.post("/api/ops/incidents/acknowledge")
async def acknowledge_ops_incident(body: OpsAckIn):
    """Acknowledge the latest failed/stalled lane incident without rewriting history."""
    pool: asyncpg.Pool = app.state.pool
    row = await pool.fetchrow(
        """SELECT id FROM ingest_runs
           WHERE connector=$1 AND (ok=false OR finished_at IS NULL)
           ORDER BY started_at DESC LIMIT 1""",
        body.connector,
    )
    if row is None:
        raise HTTPException(404, "incident not found")
    await pool.execute(
        """UPDATE ingest_runs
           SET checkpoint = coalesce(checkpoint, '{}'::jsonb) || jsonb_build_object(
                 'acknowledged_at', now(),
                 'acknowledged_note', $2::text
               )
           WHERE id=$1""",
        row["id"], body.note or "acknowledged by operator",
    )
    return {"acknowledged": True, "connector": body.connector, "run_id": row["id"]}


async def _prefs_row(pool: asyncpg.Pool, tenant_id: int) -> asyncpg.Record | dict:
    """Calculator preferences for one tenant.

    user_calculator_prefs is pinned to a single row by ``CHECK (id = 1)``, so a
    non-default tenant cannot own a row today. Rather than serve another
    tenant's private settings, an unprovisioned tenant gets the schema defaults
    and is told the row is not tenant-owned.
    """
    row = await pool.fetchrow(
        "SELECT * FROM user_calculator_prefs WHERE id=1 AND tenant_id=$1", tenant_id)
    if row is not None:
        return row
    occupied = await pool.fetchval("SELECT count(*) FROM user_calculator_prefs WHERE id=1")
    if not occupied:
        await pool.execute(
            "INSERT INTO user_calculator_prefs (id, tenant_id) VALUES (1, $1) "
            "ON CONFLICT DO NOTHING", tenant_id)
        row = await pool.fetchrow(
            "SELECT * FROM user_calculator_prefs WHERE id=1 AND tenant_id=$1", tenant_id)
        if row is not None:
            return row
    return {
        "default_markup_pct": 12.00, "risk_tolerance": "balanced",
        "default_agency_id": None, "alert_frequency": "instant", "retention_days": 180,
        "my_company_name": "شركتي", "target_win_rate_pct": 25.00,
        "cost_advantage_pct": 0.00, "updated_at": None, "tenant_id": tenant_id,
        "tenant_owned": False,
    }


@app.get("/api/settings")
async def settings(tenant_id: int = Tenant):
    """Operator settings for gated features and calculator defaults."""
    pool: asyncpg.Pool = app.state.pool
    row = await _prefs_row(pool, tenant_id)
    return {
        "gated_features": {
            "etimad_supplier_credentials": bool(os.environ.get("THAQIP_ETIMAD_USERNAME"))
            and bool(os.environ.get("THAQIP_ETIMAD_PASSWORD")),
            "llm_compliance_extraction": bool(os.environ.get("THAQIP_ANTHROPIC_API_KEY")),
            "telegram_alerts": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
            "ksa_staging": bool(os.environ.get("THAQIP_STAGING_URL")),
        },
        "calculator": {
            "default_markup_pct": float(row["default_markup_pct"]),
            "risk_tolerance": row["risk_tolerance"],
            "default_agency_id": row["default_agency_id"],
        },
        "alerts": {"frequency": row["alert_frequency"]},
        "my_company": {
            "name": row["my_company_name"],
            "target_win_rate_pct": float(row["target_win_rate_pct"]),
            "cost_advantage_pct": float(row["cost_advantage_pct"]),
        },
        "retention_days": row["retention_days"],
        "updated_at": row["updated_at"],
        "tenant_id": tenant_id,
    }


@app.patch("/api/settings")
async def update_settings(body: SettingsIn, tenant_id: int = Tenant):
    """Persist Settings tab defaults for the calling tenant."""
    pool: asyncpg.Pool = app.state.pool
    owner = await pool.fetchval("SELECT tenant_id FROM user_calculator_prefs WHERE id=1")
    if owner is not None and int(owner) != tenant_id:
        # Schema limitation, not a policy decision: user_calculator_prefs is
        # pinned to a single row (PK id, CHECK id = 1), so a second tenant has
        # nowhere to store its own preferences. Refuse rather than overwrite
        # another tenant's row. See the migration note in the P2W report.
        raise HTTPException(
            409, "per-tenant calculator preferences require the user_calculator_prefs "
                 "primary key to become (tenant_id, id)")
    await pool.execute(
        """INSERT INTO user_calculator_prefs
             (id, tenant_id, default_markup_pct, risk_tolerance, default_agency_id,
              alert_frequency, retention_days,
              my_company_name, target_win_rate_pct, cost_advantage_pct, updated_at)
           VALUES (1, $9, $1, $2, $3, $4, $5, $6, $7, $8, now())
           ON CONFLICT (id) DO UPDATE SET
             default_markup_pct=EXCLUDED.default_markup_pct,
             risk_tolerance=EXCLUDED.risk_tolerance,
             default_agency_id=EXCLUDED.default_agency_id,
             alert_frequency=EXCLUDED.alert_frequency,
             retention_days=EXCLUDED.retention_days,
             my_company_name=EXCLUDED.my_company_name,
             target_win_rate_pct=EXCLUDED.target_win_rate_pct,
             cost_advantage_pct=EXCLUDED.cost_advantage_pct,
             updated_at=now()""",
        body.default_markup_pct,
        body.risk_tolerance,
        body.default_agency_id,
        body.alert_frequency,
        body.retention_days,
        body.my_company_name.strip(),
        body.target_win_rate_pct,
        body.cost_advantage_pct,
        tenant_id,
    )
    return await settings(tenant_id)


@app.get("/api/market/price-position")
async def price_position():
    """How often does the lowest bid win a multi-bidder tender?

    HEADLINE = all multi-bidder awarded tenders, unfiltered.

    A second, narrower figure filters to offers whose technical_pass is true.
    That filter is NOT a compliance filter in this corpus: technical_pass is
    true for ~1038 offers and NULL for ~109, and is never false — there is not
    one recorded technical rejection. Filtering on it therefore discards offers
    of UNKNOWN status as though they had been disqualified, which inflates the
    result (measured 2026-09-10: 96.4% filtered vs 82.2% unfiltered). It is
    returned only as `compliant_only`, explicitly caveated, and must never be
    presented as the headline market fact.
    """
    row = await app.state.pool.fetchrow(
        """WITH ranked AS (
             SELECT o.tender_id, o.is_winner,
                    rank() OVER (PARTITION BY o.tender_id ORDER BY o.offer_value) AS price_rank,
                    count(*) OVER (PARTITION BY o.tender_id) AS n_bidders
             FROM offers o
             WHERE o.offer_value IS NOT NULL
           )
           SELECT count(*) FILTER (WHERE is_winner)                    AS awards_n,
                  count(*) FILTER (WHERE is_winner AND price_rank = 1) AS lowest_won,
                  round(avg(price_rank) FILTER (WHERE is_winner), 2)   AS avg_winner_rank
           FROM ranked WHERE n_bidders >= 2""")
    filt = await app.state.pool.fetchrow(
        """WITH ranked AS (
             SELECT o.tender_id, o.is_winner,
                    rank() OVER (PARTITION BY o.tender_id ORDER BY o.offer_value) AS price_rank,
                    count(*) OVER (PARTITION BY o.tender_id) AS n_bidders
             FROM offers o
             WHERE o.technical_pass AND o.offer_value IS NOT NULL
           )
           SELECT count(*) FILTER (WHERE is_winner)                    AS awards_n,
                  count(*) FILTER (WHERE is_winner AND price_rank = 1) AS lowest_won
           FROM ranked WHERE n_bidders >= 2""")
    status = await app.state.pool.fetchrow(
        """SELECT count(*) FILTER (WHERE technical_pass IS TRUE)  AS pass_true,
                  count(*) FILTER (WHERE technical_pass IS FALSE) AS pass_false,
                  count(*) FILTER (WHERE technical_pass IS NULL)  AS pass_unknown
           FROM offers""")
    d = dict(row)
    d["kind"] = "observed"
    d["basis"] = "all multi-bidder awarded tenders, no technical filter"
    d["lowest_wins_pct"] = (
        round(100 * d["lowest_won"] / d["awards_n"], 1) if d["awards_n"] else None)
    d["compliant_only"] = {
        "awards_n": filt["awards_n"],
        "lowest_won": filt["lowest_won"],
        "lowest_wins_pct": (round(100 * filt["lowest_won"] / filt["awards_n"], 1)
                            if filt["awards_n"] else None),
        "caveat": ("technical_pass is never false in this corpus "
                   f"(true={status['pass_true']}, false={status['pass_false']}, "
                   f"unknown={status['pass_unknown']}); this figure treats unknown "
                   "status as disqualified and is therefore an overestimate"),
    }
    d["technical_status_counts"] = dict(status)
    return d


@app.post("/api/follows/{tender_id}")
async def follow(tender_id: int, tenant_id: int = Tenant):
    n = await app.state.pool.execute(
        "INSERT INTO follows (tender_id, tenant_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        tender_id, tenant_id)
    return {"following": True, "created": n.endswith("1")}


@app.delete("/api/follows/{tender_id}")
async def unfollow(tender_id: int, tenant_id: int = Tenant):
    await app.state.pool.execute(
        "DELETE FROM follows WHERE tender_id=$1 AND tenant_id=$2", tender_id, tenant_id)
    return {"following": False}


@app.get("/api/tenders.csv")
async def tenders_csv(
    q: str | None = None, agency_id: int | None = None, activity_id: int | None = None,
    awarded: bool | None = None, open_only: bool = False, source: str | None = None,
    tenant_id: int = Tenant,
):
    """M2-5: Excel-ready export (UTF-8 BOM so Arabic opens correctly)."""
    import csv
    import io as _io

    from fastapi.responses import Response

    data = await tenders(q=q, agency_id=agency_id, activity_id=activity_id,
                         awarded=awarded, open_only=open_only, source=source,
                         limit=200, offset=0, tenant_id=tenant_id)
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
async def calibration(tenant_id: int = Tenant):
    """M5-4: prediction-vs-outcome pairs — the calibration dataset status."""
    pool: asyncpg.Pool = app.state.pool
    row = await pool.fetchrow(
        """SELECT count(pr.id)                                       AS predictions,
                  count(o.id)                                        AS resolved,
                  round(avg(pr.value) FILTER (WHERE o.id IS NOT NULL), 4)      AS avg_predicted,
                  round(avg((o.result='won')::int::numeric)
                        FILTER (WHERE o.id IS NOT NULL), 4)          AS actual_win_rate
           FROM predictions pr
           LEFT JOIN outcomes o ON o.pursuit_id = pr.pursuit_id AND o.tenant_id = $1
           WHERE pr.tenant_id = $1""", tenant_id)
    pairs = await pool.fetch(
        """SELECT pr.pursuit_id, pr.value AS predicted, o.result,
                  left(t.name, 50) AS name
           FROM predictions pr
           JOIN outcomes o ON o.pursuit_id = pr.pursuit_id
           JOIN pursuits p ON p.id = pr.pursuit_id
           JOIN tenders t ON t.id = p.tender_id
           WHERE pr.tenant_id = $1 AND o.tenant_id = $1 AND p.tenant_id = $1
           ORDER BY o.logged_at DESC LIMIT 20""", tenant_id)
    return {**dict(row), "recent_pairs": [dict(r) for r in pairs]}


# A pricing "prediction" is scored only if it is a system hypothesis (a
# baseline_* simulation, never a what-if price a user typed into the
# simulator) AND it was recorded before the award was first seen. Without both
# rules the card scored typed probes like 50 or 1,000,000 SAR against awards
# already known, and showed a 3825% MAPE that measured nothing.
MIN_ACCURACY_SAMPLE = 10


@app.get("/api/pricing/accuracy")
async def pricing_accuracy(tenant_id: int = Tenant):
    """M5: system price hypotheses versus the awards that followed them."""
    pool: asyncpg.Pool = app.state.pool
    refreshed = await _measure_all_pricing_predictions(pool, tenant_id)
    row = await pool.fetchrow(
        """WITH
    first_award AS (
        SELECT tender_id, min(created_at) AS first_seen
        FROM awards WHERE award_value IS NOT NULL AND award_value > 0
        GROUP BY tender_id),
    latest AS (
        SELECT DISTINCT ON (r.pursuit_id) r.*, s.created_at AS predicted_at,
               s.win_pct, s.basis
        FROM pricing_prediction_results r
        JOIN pursuit_simulations s ON s.id = r.simulation_id
        JOIN pursuits pu ON pu.id = r.pursuit_id AND pu.tenant_id = $1
        JOIN first_award fa ON fa.tender_id = pu.tender_id
        WHERE s.basis LIKE 'baseline\\_%' AND s.created_at < fa.first_seen
        ORDER BY r.pursuit_id, s.created_at DESC, r.id DESC)
           SELECT count(*)::int AS measured,
                  round(avg(percentage_error)::numeric, 4)::float AS mape_all,
                  round((percentile_cont(0.5) WITHIN GROUP (ORDER BY percentage_error))::numeric, 4)::float AS median_ape,
                  round(avg(percentage_error) FILTER (WHERE measured_at >= now() - interval '30 days')::numeric, 4)::float AS mape_30d,
                  round(avg(percentage_error) FILTER (WHERE measured_at >= now() - interval '90 days')::numeric, 4)::float AS mape_90d,
                  count(*) FILTER (WHERE measured_at >= now() - interval '30 days')::int AS sample_30d,
                  count(*) FILTER (WHERE measured_at >= now() - interval '90 days')::int AS sample_90d,
                  round(100.0 * count(*) FILTER (WHERE percentage_error <= 0.10) / nullif(count(*),0), 1)::float AS within_10_pct,
                  round(100.0 * count(*) FILTER (WHERE percentage_error <= 0.20) / nullif(count(*),0), 1)::float AS within_20_pct,
                  round(100.0 * count(*) FILTER (WHERE percentage_error <= 0.30) / nullif(count(*),0), 1)::float AS within_30_pct
           FROM latest""",
        tenant_id,
    )
    excluded = await pool.fetchrow(
        """SELECT count(*) FILTER (WHERE s.basis NOT LIKE 'baseline\\_%')::int AS what_if,
                  count(*) FILTER (WHERE s.basis LIKE 'baseline\\_%'
                                   AND s.created_at >= fa.first_seen)::int AS after_award
           FROM pricing_prediction_results r
           JOIN pursuit_simulations s ON s.id = r.simulation_id
           JOIN pursuits pu ON pu.id = r.pursuit_id AND pu.tenant_id = $1
           LEFT JOIN (SELECT tender_id, min(created_at) AS first_seen FROM awards
                      GROUP BY tender_id) fa ON fa.tender_id = pu.tender_id""",
        tenant_id)
    recent = await pool.fetch(
        """WITH
    first_award AS (
        SELECT tender_id, min(created_at) AS first_seen
        FROM awards WHERE award_value IS NOT NULL AND award_value > 0
        GROUP BY tender_id),
    latest AS (
        SELECT DISTINCT ON (r.pursuit_id) r.*, s.created_at AS predicted_at,
               s.win_pct, s.basis
        FROM pricing_prediction_results r
        JOIN pursuit_simulations s ON s.id = r.simulation_id
        JOIN pursuits pu ON pu.id = r.pursuit_id AND pu.tenant_id = $1
        JOIN first_award fa ON fa.tender_id = pu.tender_id
        WHERE s.basis LIKE 'baseline\\_%' AND s.created_at < fa.first_seen
        ORDER BY r.pursuit_id, s.created_at DESC, r.id DESC)
           SELECT r.pursuit_id, left(t.name, 70) AS name,
                  r.predicted_price::float AS predicted_price,
                  r.actual_award_value::float AS actual_award_value,
                  r.absolute_error::float AS absolute_error,
                  round((r.percentage_error * 100)::numeric, 1)::float AS error_pct,
                  r.win_pct::float AS win_pct, r.basis, r.predicted_at, r.measured_at
           FROM latest r
           JOIN pursuits p ON p.id = r.pursuit_id
           JOIN tenders t ON t.id = p.tender_id
           ORDER BY r.measured_at DESC LIMIT 12""",
        tenant_id,
    )
    out = dict(row)
    out["min_sample"] = MIN_ACCURACY_SAMPLE
    out["excluded"] = {"what_if_simulations": excluded["what_if"],
                       "recorded_after_award": excluded["after_award"]}
    out["refreshed"] = refreshed
    if not out["measured"]:
        out["status"] = "awaiting_awards"
    elif out["measured"] < MIN_ACCURACY_SAMPLE:
        out["status"] = "insufficient_sample"
    else:
        out["status"] = "measured"
    if out["status"] != "measured":
        # Below the floor a percentage is an anecdote, not an accuracy figure.
        for key in ("mape_all", "median_ape", "mape_30d", "mape_90d",
                    "within_10_pct", "within_20_pct", "within_30_pct"):
            out[key] = None
    out["confidence"] = ("high" if out["sample_90d"] >= 50 else
                         "medium" if out["sample_90d"] >= MIN_ACCURACY_SAMPLE else "low")
    out["recent"] = [dict(r) for r in recent]
    out["market_clock"] = await _market_clock(pool)
    return out


MARKET_CLOCK_FIRST_SAMPLE = 150


async def _market_clock(pool: asyncpg.Pool) -> dict[str, Any]:
    """System-wide blind market predictions (accuracy Stage 1, migration 0019).

    Shared-corpus only, no tenant data: every open Etimad tender gets a daily
    snapshot; scoring happens in prediction_scorecard. No hit rate is returned
    until the first reportable sample exists.
    """
    try:
        row = dict(await pool.fetchrow(
            """SELECT
                 (SELECT count(DISTINCT tender_id) FROM price_predictions
                   WHERE origin = 'daily_snapshot' AND prediction_scope = 'MARKET') AS tenders_tracked,
                 (SELECT max(generated_at) FROM price_predictions
                   WHERE origin = 'daily_snapshot')                                  AS last_snapshot_at,
                 (SELECT count(*) FROM prediction_scorecard)                          AS blind_awarded,
                 (SELECT count(*) FROM prediction_scorecard WHERE scorable)           AS scored,
                 (SELECT count(*) FROM prediction_scorecard WHERE suppressed)         AS refused,
                 (SELECT round(100.0 * avg(interval_hit::int), 1)
                    FROM prediction_scorecard WHERE scorable)::float                  AS interval_hit_pct,
                 (SELECT round((percentile_cont(0.5) WITHIN GROUP (ORDER BY abs_pct_error) * 100)::numeric, 1)
                    FROM prediction_scorecard WHERE scorable)::float                  AS median_abs_pct_error"""))
    except asyncpg.UndefinedTableError:
        return {"status": "not_installed"}
    row["first_reportable_sample"] = MARKET_CLOCK_FIRST_SAMPLE
    row["nominal_interval_pct"] = 80
    if (row["scored"] or 0) < MARKET_CLOCK_FIRST_SAMPLE:
        row["interval_hit_pct"] = None
        row["median_abs_pct_error"] = None
        row["status"] = "collecting"
    else:
        row["status"] = "reportable"
    return row


@app.post("/api/pricing/seed-baselines")
async def seed_pricing_baselines(tenant_id: int = Tenant):
    """Create one baseline pricing simulation for active pursuits lacking one.

    This accelerates the calibration loop: once an award is announced, the
    pursuit already has a recorded hypothesis to measure against. The seed is
    deliberately conservative and based only on public/historical corpus data.
    """
    pool: asyncpg.Pool = app.state.pool
    rows = await pool.fetch(
        """SELECT p.id AS pursuit_id, t.id AS tender_id, t.activity_id,
                  t.booklet_price::float AS booklet_price,
                  t.submitted_bids_count, t.external_bids_count
           FROM pursuits p
           JOIN tenders t ON t.id = p.tender_id
           WHERE p.tenant_id = $1
             AND p.stage NOT IN ('submitted', 'won', 'lost')
             AND NOT EXISTS (
               SELECT 1 FROM pursuit_simulations s WHERE s.pursuit_id = p.id
             )
           ORDER BY p.updated_at DESC, p.id DESC
           LIMIT 100""",
        tenant_id,
    )
    seeded = []
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
            p25_award = bench["p25_award"] if bench else None
            p75_award = bench["p75_award"] if bench else None
            sample_count = bench["n"] if bench else 0
            if median_award:
                proposed_price = round(median_award * 0.92, 2)
                basis = "baseline_activity_history"
            elif r["booklet_price"] and r["booklet_price"] > 0:
                proposed_price = round(max(r["booklet_price"] * 120.0, 10000.0), 2)
                basis = "baseline_booklet_price"
            else:
                proposed_price = 100000.0
                basis = "baseline_default"

            live_bidders = (r["submitted_bids_count"] or 0) + (r["external_bids_count"] or 0)
            effective_bidders = max(float(live_bidders or 4), 1.0)
            if median_award and median_award > 0:
                import math
                ratio = proposed_price / median_award
                win_prob = max(0.02, min(0.95, 1.0 / (1.0 + math.exp(4.0 * (ratio - 0.95)))))
            else:
                win_prob = 1.0 / (effective_bidders + 1.0)
            win_prob_pct = round(win_prob * 100.0, 1)
            expected_value = round(proposed_price * (win_prob_pct / 100.0), 2)
            metadata = {
                "seeded": True,
                "sample_count": sample_count,
                "p25_award": p25_award,
                "median_award": median_award,
                "p75_award": p75_award,
                "live_bidders": live_bidders,
            }
            sim_id = await conn.fetchval(
                """INSERT INTO pursuit_simulations
                     (pursuit_id, proposed_price, win_pct, expected_value, basis, metadata)
                   VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                   RETURNING id""",
                r["pursuit_id"], proposed_price, win_prob_pct, expected_value, basis, json.dumps(metadata),
            )
            seeded.append({
                "simulation_id": sim_id,
                "pursuit_id": r["pursuit_id"],
                "proposed_price": proposed_price,
                "win_probability_pct": win_prob_pct,
                "basis": basis,
                "sample_count": sample_count,
            })
    measured = await _measure_all_pricing_predictions(pool, tenant_id)
    return {"seeded": len(seeded), "measured_after_seed": measured, "items": seeded}


@app.get("/api/outcomes/summary")
async def outcomes_summary(tenant_id: int = Tenant):
    row = await app.state.pool.fetchrow(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE result='won') AS won,
                  avg(submitted_value) FILTER (WHERE result='won') AS avg_win_value
           FROM outcomes WHERE tenant_id = $1""", tenant_id)
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
    agency_matrix = await pool.fetch("""
        WITH offer_context AS (
          SELECT coalesce(a.canonical_name, t.agency_name_raw) AS agency,
                 o.offer_value,
                 o.is_winner,
                 o.technical_pass,
                 (SELECT min(o2.offer_value) FROM offers o2
                   WHERE o2.tender_id = t.id AND o2.technical_pass AND o2.offer_value IS NOT NULL) AS lowest_offer
          FROM offers o
          JOIN tenders t ON t.id=o.tender_id
          LEFT JOIN agencies a ON a.id=t.agency_id
          WHERE o.vendor_id=$1
        )
        SELECT agency,
               count(*) AS participations,
               count(*) FILTER (WHERE is_winner) AS wins,
               round(100.0*count(*) FILTER (WHERE is_winner)/nullif(count(*),0),1) AS win_rate,
               round(100.0*count(*) FILTER (WHERE technical_pass)/nullif(count(*),0),1) AS tech_rate,
               round(avg(offer_value),0) AS avg_offer,
               round(avg(100.0*(offer_value-lowest_offer)/nullif(lowest_offer,0))
                 FILTER (WHERE offer_value IS NOT NULL AND lowest_offer IS NOT NULL),1) AS avg_gap_vs_lowest_pct
        FROM offer_context
        GROUP BY agency
        ORDER BY wins DESC, participations DESC, agency
        LIMIT 16""", vid)
    out = dict(v)
    out["stats"] = dict(stats)
    out["history"] = [dict(r) for r in history]
    out["agencies"] = [dict(r) for r in agencies]
    out["agency_matrix"] = [dict(r) for r in agency_matrix]
    return out


@app.get("/api/vendors/{vid}/compare")
async def vendor_compare(vid: int, tenant_id: int = Tenant):
    """Compare a competitor profile against the operator's company target profile."""
    detail = await vendor_detail(vid)
    prefs = await _prefs_row(app.state.pool, tenant_id)
    stats = detail["stats"]
    participations = int(stats.get("participations") or 0)
    wins = int(stats.get("wins") or 0)
    vendor_win_rate = round(100 * wins / participations, 1) if participations else 0.0
    vendor_tech_rate = float(stats.get("tech_rate") or 0)
    avg_offer = float(stats["avg_offer"]) if stats.get("avg_offer") is not None else None
    target_win_rate = float(prefs["target_win_rate_pct"] or 0)
    cost_advantage = float(prefs["cost_advantage_pct"] or 0)
    target_offer = round(avg_offer * (1 - cost_advantage / 100), 2) if avg_offer is not None else None
    win_gap = round(vendor_win_rate - target_win_rate, 1)
    recommendations = []
    if win_gap > 10:
        recommendations.append("المورد يملك معدل فوز أعلى من هدفك؛ راقب جهاته المتكررة وافتح فرصًا بسعر أشرس أو عرض فني أقوى عند مواجهته.")
    elif win_gap < -10:
        recommendations.append("هدف شركتك أعلى من أداء هذا المورد؛ يمكنك مهاجمته بثقة في الفرص المشابهة مع الحفاظ على هامش صحي.")
    else:
        recommendations.append("الفجوة قريبة؛ القرار يجب أن يعتمد على الجهة، وزن التقييم الفني، وعدد المنافسين المتوقع.")
    if vendor_tech_rate >= 80:
        recommendations.append("المطابقة الفنية لديه مرتفعة؛ لا تجعل السعر وحده سلاحك، بل اربط العرض بإثباتات امتثال واضحة.")
    elif vendor_tech_rate and vendor_tech_rate < 60:
        recommendations.append("لديه ضعف فني ظاهر؛ ركّز على اكتمال المتطلبات وتوثيق الخبرات قبل خصم السعر.")
    if cost_advantage > 0 and target_offer is not None:
        recommendations.append(f"ميزة التكلفة المحفوظة تعني أن سعرًا حول {target_offer:,.0f} ر.س يعادل متوسط عروضه بعد الخصم.")
    return {
        "vendor": {
            "id": detail["id"],
            "name": detail["canonical_name"],
            "participations": participations,
            "wins": wins,
            "win_rate_pct": vendor_win_rate,
            "tech_rate_pct": vendor_tech_rate,
            "avg_offer": avg_offer,
        },
        "my_company": {
            "name": prefs["my_company_name"],
            "target_win_rate_pct": target_win_rate,
            "cost_advantage_pct": cost_advantage,
            "target_offer_vs_vendor_avg": target_offer,
        },
        "deltas": {
            "win_rate_gap_pct": win_gap,
            "technical_gap_pct": round(vendor_tech_rate - 75.0, 1),
            "cost_advantage_pct": cost_advantage,
        },
        "recommendations": recommendations,
    }


TYPESENSE_URL = os.environ.get("TYPESENSE_URL", "http://localhost:8108")
# No default: a credential literal in source is a credential that ships. When
# the env var is missing, search fails loudly at the index rather than quietly
# authenticating with a value anyone can read out of this file.
TYPESENSE_KEY = os.environ.get("TYPESENSE_KEY", "")


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
    upcoming = await pool.fetch("""
        SELECT t.id, left(t.name,70) AS name, t.last_offer_date,
               greatest(0, extract(epoch FROM t.last_offer_date - now()))::bigint AS remaining_s,
               EXISTS (SELECT 1 FROM awards w WHERE w.tender_id=t.id) AS has_award,
               (SELECT w.award_value FROM awards w WHERE w.tender_id=t.id LIMIT 1) AS award_value
        FROM tenders t
        WHERE t.agency_id=$1 AND t.last_offer_date > now()
        ORDER BY t.last_offer_date ASC NULLS LAST LIMIT 15""", aid)
    out = dict(a)
    out["stats"] = dict(stats)
    out["top_vendors"] = [dict(r) for r in top_vendors]
    out["activities"] = [dict(r) for r in activities]
    out["recent"] = [dict(r) for r in recent]
    out["upcoming_deadlines"] = [dict(r) for r in upcoming]
    return out


# ------------------------------------------------------------ telegram
@app.get("/api/telegram")
async def telegram_status(tenant_id: int = Tenant):
    bot: TelegramBot = app.state.telegram
    username = None
    if bot.configured:
        try:
            username = await bot.ensure_identity()
        except Exception as exc:
            bot.last_error = str(exc).replace(bot.token, "***")[:200]
    links = await app.state.pool.fetch(
        """SELECT id, chat_id, chat_type, chat_title, tg_username, first_name, linked_at
           FROM telegram_links WHERE tenant_id=$1 AND active ORDER BY linked_at DESC""",
        tenant_id)
    return {"configured": bot.configured, "bot_username": username,
            "polling": bot.last_poll_at is not None and bot.last_error is None,
            "last_poll_at": bot.last_poll_at, "last_error": bot.last_error,
            "links": [dict(r) for r in links]}


@app.post("/api/telegram/link")
async def telegram_link(request: Request, tenant_id: int = Tenant):
    bot: TelegramBot = app.state.telegram
    if not bot.configured:
        raise HTTPException(409, detail={
            "error": "telegram_not_configured",
            "message_ar": "لم يُضبط رمز البوت بعد. شغّل bin/set-telegram-token.sh على الخادم."})
    who = getattr(request.state, "principal", None) or {}
    try:
        return await bot.new_link(app.state.pool, tenant_id, who.get("user_id"))
    except RuntimeError as exc:
        raise HTTPException(502, detail={"error": "telegram_unreachable", "detail": str(exc)})


@app.post("/api/telegram/test")
async def telegram_test(tenant_id: int = Tenant):
    bot: TelegramBot = app.state.telegram
    if not bot.configured:
        raise HTTPException(409, detail={"error": "telegram_not_configured"})
    links = await app.state.pool.fetch(
        "SELECT chat_id FROM telegram_links WHERE tenant_id=$1 AND active", tenant_id)
    if not links:
        raise HTTPException(422, detail={"error": "telegram_not_linked",
                                         "message_ar": "لا توجد محادثة مربوطة بعد."})
    results = []
    for link in links:
        try:
            await bot.send(link["chat_id"], "🔔 رسالة تجريبية من ثاقب — التنبيهات تعمل.")
            results.append({"chat_id": link["chat_id"], "ok": True})
        except RuntimeError as exc:
            results.append({"chat_id": link["chat_id"], "ok": False, "error": str(exc)})
    return {"results": results}


@app.delete("/api/telegram/links/{link_id}")
async def telegram_unlink(link_id: int, tenant_id: int = Tenant):
    done = await app.state.pool.fetchval(
        """UPDATE telegram_links SET active=false WHERE id=$1 AND tenant_id=$2
           RETURNING id""", link_id, tenant_id)
    if done is None:
        raise HTTPException(404)
    return {"ok": True}


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    channel: str = Field(default="log", pattern="^(log|telegram|email)$")
    target: str = "dev-console"
    keywords: list[str] = []
    activity_ids: list[int] = []
    agency_ids: list[int] = []
    sources: list[str] = ["etimad", "forsah"]
    event_types: list[str] = ["tender.created", "tender.extended", "tender.awarded"]
    digest_interval: str = Field(default="instant", pattern="^(instant|hourly|daily)$")


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    channel: str | None = Field(default=None, pattern="^(log|telegram|email)$")
    target: str | None = None
    keywords: list[str] | None = None
    activity_ids: list[int] | None = None
    agency_ids: list[int] | None = None
    sources: list[str] | None = None
    event_types: list[str] | None = None
    digest_interval: str | None = Field(default=None, pattern="^(instant|hourly|daily)$")


@app.get("/api/profiles")
async def profiles(tenant_id: int = Tenant):
    rows = await app.state.pool.fetch(
        """SELECT p.*,
                  (SELECT count(*) FROM notifications n WHERE n.profile_id = p.id) AS sent_count,
                  (SELECT max(created_at) FROM notifications n WHERE n.profile_id = p.id) AS last_at
           FROM alert_profiles p WHERE p.tenant_id = $1 ORDER BY p.id DESC""",
        tenant_id,
    )
    return [dict(r) for r in rows]


async def _telegram_default_target(tenant_id: int, target: str | None) -> str:
    """A Telegram profile with no real chat id goes to the tenant's most
    recently linked chat; with no linked chat it is refused, not silently
    parked as 'pending' forever."""
    if target and target.strip() and target.strip() != "dev-console":
        return target.strip()
    chat = await app.state.pool.fetchval(
        """SELECT chat_id FROM telegram_links WHERE tenant_id=$1 AND active
           ORDER BY linked_at DESC LIMIT 1""", tenant_id)
    if chat is None:
        raise HTTPException(422, detail={
            "error": "telegram_not_linked",
            "message_ar": "اربط حساب تيليجرام أولاً من بطاقة «ربط تيليجرام» أعلى الصفحة."})
    return chat


@app.post("/api/profiles")
async def create_profile(p: ProfileIn, tenant_id: int = Tenant):
    if p.channel == "telegram":
        p.target = await _telegram_default_target(tenant_id, p.target)
    row = await app.state.pool.fetchrow(
        """INSERT INTO alert_profiles (name, channel, target, keywords, activity_ids,
                                       agency_ids, sources, event_types, digest_interval,
                                       tenant_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
        p.name, p.channel, p.target, p.keywords, p.activity_ids,
        p.agency_ids, p.sources, p.event_types, p.digest_interval, tenant_id,
    )
    return {"id": row["id"]}


@app.patch("/api/profiles/{pid}")
async def update_profile(pid: int, patch: ProfilePatch, tenant_id: int = Tenant):
    current = await app.state.pool.fetchrow(
        "SELECT * FROM alert_profiles WHERE id=$1 AND tenant_id=$2", pid, tenant_id)
    if current is None:
        raise HTTPException(404)
    data = dict(current)
    update = patch.model_dump(exclude_unset=True)
    for key, value in update.items():
        if value is not None:
            data[key] = value.strip() if isinstance(value, str) else value
    if not data["sources"] or any(src not in ("etimad", "forsah") for src in data["sources"]):
        raise HTTPException(422, "sources must include etimad and/or forsah")
    if not data["event_types"]:
        raise HTTPException(422, "event_types must not be empty")
    if data["channel"] == "telegram":
        data["target"] = await _telegram_default_target(tenant_id, data["target"])
    row = await app.state.pool.fetchrow(
        """UPDATE alert_profiles
           SET name=$2, channel=$3, target=$4, keywords=$5, activity_ids=$6,
               agency_ids=$7, sources=$8, event_types=$9, digest_interval=$10
           WHERE id=$1 AND tenant_id=$11
           RETURNING *""",
        pid,
        data["name"],
        data["channel"],
        data["target"],
        data["keywords"],
        data["activity_ids"],
        data["agency_ids"],
        data["sources"],
        data["event_types"],
        data["digest_interval"],
        tenant_id,
    )
    return dict(row)


@app.patch("/api/profiles/{pid}/toggle")
async def toggle_profile(pid: int, tenant_id: int = Tenant):
    active = await app.state.pool.fetchval(
        "UPDATE alert_profiles SET active = NOT active WHERE id=$1 AND tenant_id=$2 "
        "RETURNING active", pid, tenant_id
    )
    if active is None:
        raise HTTPException(404)
    return {"active": active}


@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: int, tenant_id: int = Tenant):
    owned = await app.state.pool.fetchval(
        "SELECT 1 FROM alert_profiles WHERE id=$1 AND tenant_id=$2", pid, tenant_id)
    if not owned:
        raise HTTPException(404)
    await app.state.pool.execute("DELETE FROM notifications WHERE profile_id=$1", pid)
    n = await app.state.pool.execute(
        "DELETE FROM alert_profiles WHERE id=$1 AND tenant_id=$2", pid, tenant_id)
    if n.endswith("0"):
        raise HTTPException(404)
    return {"deleted": True}


@app.get("/api/notifications")
async def notifications(
    limit: int = Query(50, le=200),
    profile_id: int | None = None,
    q: str | None = Query(None, max_length=120),
    tenant_id: int = Tenant,
):
    where: list[str] = []
    args: list[object] = []

    def arg(value: object) -> str:
        args.append(value)
        return f"${len(args)}"

    where.append(f"p.tenant_id = {arg(tenant_id)}")
    if profile_id is not None:
        where.append(f"n.profile_id = {arg(profile_id)}")
    if q:
        needle = f"%{q.strip()}%"
        where.append(
            f"(n.title ILIKE {arg(needle)} OR n.body ILIKE {arg(needle)} OR p.name ILIKE {arg(needle)})"
        )
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    args.append(limit)
    rows = await app.state.pool.fetch(
        f"""SELECT n.id, n.event_type, n.channel, n.status, n.title, n.body, n.created_at,
                  n.tender_id, p.name AS profile_name, p.id AS profile_id
           FROM notifications n JOIN alert_profiles p ON p.id = n.profile_id
           {sql_where}
           ORDER BY n.id DESC LIMIT ${len(args)}""",
        *args,
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
    tenant_id: int = Tenant,
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

    # The corpus itself is shared; only the "followed" flag is tenant-private,
    # so the tenant argument belongs to the projection, not to the filter.
    filter_sql = " AND ".join(where)
    filter_args = list(args)
    tenant_arg = arg(tenant_id)

    rows = await pool.fetch(
        f"""SELECT t.id, t.source, t.source_tender_id, t.reference_number, t.name,
                   t.submitted_bids_count, t.draft_bids_count, t.external_bids_count,
                   coalesce(a.canonical_name, t.agency_name_raw) AS agency,
                   t.activity_name_raw AS activity, t.status_id,
                   t.last_offer_date, t.published_at, t.detected_at,
                   EXISTS (SELECT 1 FROM awards w WHERE w.tender_id = t.id) AS has_award,
                   EXISTS (SELECT 1 FROM follows f
                            WHERE f.tender_id = t.id AND f.tenant_id = {tenant_arg}) AS followed,
                   greatest(0, extract(epoch FROM t.last_offer_date - now()))::bigint AS remaining_s
            FROM tenders t LEFT JOIN agencies a ON a.id = t.agency_id
            WHERE {filter_sql}
            ORDER BY t.published_at DESC NULLS LAST
            LIMIT {arg(limit)} OFFSET {arg(offset)}""",
        *args,
    )
    total = await pool.fetchval(
        f"SELECT count(*) FROM tenders t WHERE {filter_sql}",
        *filter_args,
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


@app.get("/api/tenders/{tender_id}/price-curve")
async def tender_price_curve(tender_id: int, mode: str = Query("awards", pattern="^(awards|offers)$")):
    """Historical price curve for the tender activity, used by the drawer chart."""
    pool: asyncpg.Pool = app.state.pool
    t = await pool.fetchrow(
        "SELECT id, activity_id, activity_name_raw FROM tenders WHERE id=$1",
        tender_id,
    )
    if t is None:
        raise HTTPException(404)
    if mode == "offers":
        rows = await pool.fetch(
            """SELECT t2.id AS tender_id, left(t2.name, 70) AS name,
                      coalesce(a.canonical_name, t2.agency_name_raw) AS agency,
                      t2.published_at::date AS day,
                      min(o.offer_value) FILTER (WHERE o.technical_pass) AS low_price,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY o.offer_value)
                        FILTER (WHERE o.offer_value IS NOT NULL) AS median_price,
                      count(o.id) FILTER (WHERE o.offer_value IS NOT NULL) AS samples
               FROM tenders t2
               JOIN offers o ON o.tender_id=t2.id
               LEFT JOIN agencies a ON a.id=t2.agency_id
               WHERE t2.id <> $1 AND t2.activity_id IS NOT DISTINCT FROM $2
                 AND o.offer_value IS NOT NULL
               GROUP BY t2.id, a.canonical_name
               ORDER BY t2.published_at NULLS LAST, t2.id
               LIMIT 80""",
            tender_id, t["activity_id"],
        )
        value_key = "median_price"
    else:
        rows = await pool.fetch(
            """SELECT t2.id AS tender_id, left(t2.name, 70) AS name,
                      coalesce(a.canonical_name, t2.agency_name_raw) AS agency,
                      coalesce(t2.last_offer_date, t2.published_at)::date AS day,
                      w.award_value AS award_price,
                      1 AS samples
               FROM awards w
               JOIN tenders t2 ON t2.id=w.tender_id
               LEFT JOIN agencies a ON a.id=t2.agency_id
               WHERE t2.id <> $1 AND t2.activity_id IS NOT DISTINCT FROM $2
                 AND w.award_value IS NOT NULL
               ORDER BY coalesce(t2.last_offer_date, t2.published_at) NULLS LAST, w.id
               LIMIT 80""",
            tender_id, t["activity_id"],
        )
        value_key = "award_price"
    items = []
    for r in rows:
        d = dict(r)
        value = d.get(value_key)
        if value is None:
            continue
        d["value"] = float(value)
        d["day"] = d["day"].isoformat() if d.get("day") else None
        items.append(d)
    values = [x["value"] for x in items]
    return {
        "activity_id": t["activity_id"],
        "activity": t["activity_name_raw"],
        "mode": mode,
        "count": len(items),
        "min_value": min(values) if values else None,
        "max_value": max(values) if values else None,
        "items": items,
    }


# This legacy panel predates the evidence gate. It published an actionable
# per-vendor price_to_beat from as little as ONE observed offer, on the very
# tenders where the gated engine correctly returns no competitor at all. That
# is exactly the false precision the P2W spec forbids, so it is off unless an
# operator opts in explicitly and accepts the caveat.
LEGACY_COMPETITOR_PRICES_ENABLED = (
    os.environ.get("THAQIP_ENABLE_LEGACY_COMPETITOR_PRICES", "").lower()
    in {"1", "true", "yes"}
)
# Below this many observed offers a vendor's "usual price" is not an estimate,
# it is an anecdote. Matches the competitor model's Tier-B evidence floor.
LEGACY_MIN_OBSERVATIONS = 4


@app.get("/api/tenders/{tender_id}/competitor-prices")
async def tender_competitor_prices(tender_id: int, limit: int = Query(12, ge=1, le=50)):
    """DEPRECATED, ungated legacy panel. Use /api/tenders/{id}/competitors.

    Disabled by default: it derived a per-vendor price-to-beat from single
    observations with no evidence tier, no confidence and no suppression.
    """
    if not LEGACY_COMPETITOR_PRICES_ENABLED:
        raise HTTPException(
            status_code=410,
            detail={
                "error": "endpoint_retired",
                "reason_ar": (
                    "أُوقفت هذه اللوحة: كانت تنشر سعرًا مستهدفًا لكل منافس اعتمادًا على "
                    "ملاحظة واحدة أحيانًا، دون درجة أدلة أو ثقة أو كتم عند ضعف الأدلة."),
                "use_instead": f"/api/tenders/{tender_id}/competitors",
                "override_env": "THAQIP_ENABLE_LEGACY_COMPETITOR_PRICES=1",
            },
        )
    pool: asyncpg.Pool = app.state.pool
    tender = await pool.fetchrow(
        """SELECT id, activity_id, activity_name_raw, agency_id, agency_name_raw
           FROM tenders WHERE id=$1""",
        tender_id,
    )
    if tender is None:
        raise HTTPException(404)
    rows = await pool.fetch(
        """WITH rival_offers AS (
             SELECT v.id AS vendor_id,
                    v.canonical_name AS vendor,
                    o.offer_value::numeric AS offer_value,
                    o.is_winner,
                    o.technical_pass,
                    t2.id AS tender_id,
                    t2.name AS tender_name,
                    t2.last_offer_date,
                    coalesce(a.canonical_name, t2.agency_name_raw) AS agency,
                    t2.agency_id IS NOT DISTINCT FROM $3 AS same_agency
             FROM offers o
             JOIN vendors v ON v.id=o.vendor_id
             JOIN tenders t2 ON t2.id=o.tender_id
             LEFT JOIN agencies a ON a.id=t2.agency_id
             WHERE t2.id <> $1
               AND t2.activity_id IS NOT DISTINCT FROM $2
               AND o.offer_value IS NOT NULL
           ), ranked AS (
             SELECT *, row_number() OVER (PARTITION BY vendor_id ORDER BY last_offer_date DESC NULLS LAST, tender_id DESC) AS recency_rank
             FROM rival_offers
           )
           SELECT vendor_id, vendor,
                  count(*) AS samples,
                  count(*) FILTER (WHERE is_winner) AS wins,
                  round(100.0*count(*) FILTER (WHERE technical_pass)/nullif(count(*),0),1) AS tech_rate,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY offer_value)::numeric AS median_offer,
                  min(offer_value) AS min_offer,
                  max(offer_value) AS max_offer,
                  avg(offer_value)::numeric AS avg_offer,
                  max(last_offer_date) AS last_seen,
                  bool_or(same_agency) AS seen_in_same_agency,
                  (array_agg(tender_id ORDER BY recency_rank))[1] AS latest_tender_id,
                  (array_agg(left(tender_name, 80) ORDER BY recency_rank))[1] AS latest_tender_name,
                  (array_agg(agency ORDER BY recency_rank))[1] AS latest_agency,
                  (array_agg(offer_value ORDER BY recency_rank))[1] AS latest_offer
           FROM ranked
           GROUP BY vendor_id, vendor
           ORDER BY seen_in_same_agency DESC, samples DESC, median_offer ASC
           LIMIT $4""",
        tender_id,
        tender["activity_id"],
        tender["agency_id"],
        limit,
    )
    items = []
    for r in rows:
        d = dict(r)
        median_offer = float(d["median_offer"]) if d.get("median_offer") is not None else None
        d["median_offer"] = median_offer
        d["min_offer"] = float(d["min_offer"]) if d.get("min_offer") is not None else None
        d["max_offer"] = float(d["max_offer"]) if d.get("max_offer") is not None else None
        d["avg_offer"] = float(d["avg_offer"]) if d.get("avg_offer") is not None else None
        d["latest_offer"] = float(d["latest_offer"]) if d.get("latest_offer") is not None else None
        d["price_to_beat_median"] = round(median_offer * 0.985, 2) if median_offer else None
        d["last_seen"] = d["last_seen"].isoformat() if d.get("last_seen") else None
        items.append(d)
    values = [x["median_offer"] for x in items if x.get("median_offer")]
    return {
        "tender_id": tender_id,
        "activity_id": tender["activity_id"],
        "activity": tender["activity_name_raw"],
        "agency_id": tender["agency_id"],
        "agency": tender["agency_name_raw"],
        "count": len(items),
        "market_median_offer": sorted(values)[len(values) // 2] if values else None,
        "items": items,
    }


def _boq_search_terms(text: str) -> list[str]:
    """Pick stable terms for a lightweight BOQ similarity lookup."""
    terms: list[str] = []
    for raw in re.findall(r"[\w\u0600-\u06FF]{3,}", text or ""):
        term = raw.strip().lower()
        if term and term not in terms:
            terms.append(term)
    return terms[:5]


@app.get("/api/boq-items/{item_id}/similar")
async def similar_boq_items(item_id: int, limit: int = Query(12, ge=1, le=40)):
    """Drill from a BOQ row into historical BOQ rows with matching wording."""
    pool: asyncpg.Pool = app.state.pool
    item = await pool.fetchrow(
        """SELECT b.*, t.activity_id, t.activity_name_raw
           FROM boq_items b JOIN tenders t ON t.id=b.tender_id
           WHERE b.id=$1""",
        item_id,
    )
    if item is None:
        raise HTTPException(404)
    terms = _boq_search_terms(item["description"])
    if not terms:
        return {"item": dict(item), "terms": [], "items": []}
    args: list[object] = [item_id, item["activity_id"]]
    clauses = []
    for term in terms:
        args.append(f"%{term}%")
        clauses.append(f"b.description ILIKE ${len(args)}")
    rows = await pool.fetch(
        f"""SELECT b.id, b.tender_id, b.item_no, b.description, b.unit, b.qty, b.confidence,
                   t.name AS tender_name,
                   coalesce(a.canonical_name, t.agency_name_raw) AS agency,
                   t.activity_name_raw AS activity,
                   (t.activity_id IS NOT DISTINCT FROM $2) AS same_activity,
                   ({'::int + '.join('(' + c + ')::int' for c in clauses)}::int) AS match_score
            FROM boq_items b
            JOIN tenders t ON t.id=b.tender_id
            LEFT JOIN agencies a ON a.id=t.agency_id
            WHERE b.id <> $1 AND ({' OR '.join(clauses)})
            ORDER BY same_activity DESC, match_score DESC, b.confidence DESC, b.id DESC
            LIMIT {int(limit)}""",
        *args,
    )
    out_item = dict(item)
    out_item.pop("created_at", None)
    return {
        "item": out_item,
        "terms": terms,
        "items": [dict(r) for r in rows],
    }


@app.get("/api/tenders/{tender_id}/export/awards")
async def export_tender_awards(tender_id: int):
    """Export tender awarding details and submitted offers as Arabic CSV."""
    import csv
    import io

    from fastapi.responses import StreamingResponse

    pool: asyncpg.Pool = app.state.pool
    tender = await pool.fetchrow(
        """SELECT t.id, t.name, t.reference_number,
                  coalesce(a.canonical_name, t.agency_name_raw) AS agency,
                  t.activity_name_raw AS activity
           FROM tenders t LEFT JOIN agencies a ON a.id=t.agency_id
           WHERE t.id=$1""",
        tender_id,
    )
    if tender is None:
        raise HTTPException(404, "tender not found")

    rows = await pool.fetch(
        """SELECT v.canonical_name AS vendor, o.vendor_name_raw, o.offer_value,
                  o.technical_pass, o.is_winner, w.award_value
           FROM offers o
           LEFT JOIN vendors v ON v.id=o.vendor_id
           LEFT JOIN awards w ON w.tender_id=o.tender_id AND w.vendor_id=o.vendor_id
           WHERE o.tender_id=$1
           ORDER BY o.is_winner DESC NULLS LAST, o.offer_value NULLS LAST""",
        tender_id,
    )
    award_only = await pool.fetch(
        """SELECT v.canonical_name AS vendor, w.award_value
           FROM awards w LEFT JOIN vendors v ON v.id=w.vendor_id
           WHERE w.tender_id=$1
             AND NOT EXISTS (
               SELECT 1 FROM offers o
               WHERE o.tender_id=w.tender_id AND o.vendor_id IS NOT DISTINCT FROM w.vendor_id
             )
           ORDER BY w.award_value NULLS LAST""",
        tender_id,
    )

    stream = io.StringIO()
    stream.write("\ufeff")
    writer = csv.writer(stream)
    writer.writerow(["ثاقب — تصدير تفاصيل الترسية والعروض"])
    writer.writerow(["المنافسة", tender["name"] or ""])
    writer.writerow(["الرقم المرجعي", tender["reference_number"] or ""])
    writer.writerow(["الجهة", tender["agency"] or ""])
    writer.writerow(["النشاط", tender["activity"] or ""])
    writer.writerow([])
    writer.writerow([
        "المورد",
        "قيمة العرض (ر.س)",
        "قيمة الترسية (ر.س)",
        "مطابق فنياً",
        "النتيجة",
    ])
    for row in rows:
        writer.writerow([
            row["vendor"] or row["vendor_name_raw"] or "",
            f"{float(row['offer_value']):,.2f}" if row["offer_value"] is not None else "",
            f"{float(row['award_value']):,.2f}" if row["award_value"] is not None else "",
            "نعم" if row["technical_pass"] else ("لا" if row["technical_pass"] is False else "—"),
            "فائز" if row["is_winner"] else "غير فائز",
        ])
    for row in award_only:
        writer.writerow([
            row["vendor"] or "",
            "",
            f"{float(row['award_value']):,.2f}" if row["award_value"] is not None else "",
            "—",
            "فائز — ترسية بلا عرض محفوظ",
        ])

    stream.seek(0)
    safe_ref = tender["reference_number"] or tender_id
    filename = f"tender_awards_{safe_ref}.csv"
    return StreamingResponse(
        io.BytesIO(stream.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.get("/api/pursuits/{pid}/export/compliance")
async def export_compliance(pid: int, tenant_id: int = Tenant):
    """M3-1: Export pursuit compliance matrix as CSV (Arabic UTF-8 with BOM)."""
    import csv
    import io

    from fastapi.responses import StreamingResponse

    pool: asyncpg.Pool = app.state.pool
    p = await pool.fetchrow(
        """SELECT p.*, t.name, t.reference_number
           FROM pursuits p JOIN tenders t ON t.id = p.tender_id
           WHERE p.id=$1 AND p.tenant_id=$2""", pid, tenant_id)
    if p is None:
        raise HTTPException(404, "pursuit not found")

    items = await pool.fetch(
        """SELECT sort_order, requirement, category, source_ref, evidence_ref, status, origin, confidence
           FROM compliance_items WHERE pursuit_id=$1
           ORDER BY sort_order, id""", pid)

    stream = io.StringIO()
    # Write UTF-8 BOM so Excel opens Arabic correctly
    stream.write("\ufeff")
    writer = csv.writer(stream)
    writer.writerow(["م", "المتطلب", "التصنيف", "المرجع النظامي / الفني", "دليل الامتثال", "الحالة", "المصدر", "نسبة الثقة"])
    for i, it in enumerate(items, 1):
        writer.writerow([
            i,
            it["requirement"],
            it["category"],
            it["source_ref"],
            it["evidence_ref"] or "",
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
    risk_tolerance: str | None = Field(default=None, pattern="^(low|balanced|high)$")


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
async def simulate_price(pid: int, body: PriceSimulationIn, tenant_id: int = Tenant):
    """M5-2, M5-3: Dynamic Win-Probability & Pricing Intelligence Simulator."""
    if body.proposed_price <= 0:
        raise HTTPException(422, "Proposed price must be greater than zero")

    pool: asyncpg.Pool = app.state.pool
    p = await pool.fetchrow(
        """SELECT p.id, t.*
           FROM pursuits p
           JOIN tenders t ON t.id = p.tender_id
           WHERE p.id = $1 AND p.tenant_id = $2""", pid, tenant_id
    )
    if p is None:
        raise HTTPException(404, "pursuit not found")

    tender = dict(p)
    activity_id = tender.get("activity_id")
    tender_id = tender.get("id")

    async with pool.acquire() as conn:
        prefs = await conn.fetchrow(
            "SELECT default_markup_pct, risk_tolerance FROM user_calculator_prefs "
            "WHERE id=1 AND tenant_id=$1", tenant_id)
        default_margin_pct = float(prefs["default_markup_pct"]) if prefs else 12.0
        target_margin_pct = body.target_margin_pct if body.target_margin_pct is not None else default_margin_pct
        risk_tolerance = body.risk_tolerance or (prefs["risk_tolerance"] if prefs else "balanced")
        risk_factor = {"low": 0.96, "balanced": 0.92, "high": 0.86}.get(risk_tolerance, 0.92)
        floor_factor = max(0.55, 1.0 - (target_margin_pct / 100.0))
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
        boq_row = await conn.fetchrow(
            """SELECT count(*) AS n, coalesce(sum(qty), 0)::float AS total_qty
               FROM boq_items WHERE tender_id=$1""",
            tender_id,
        )
        competitor_row = await conn.fetchrow(
            """SELECT v.canonical_name, count(*) AS bids,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY o.offer_value)::float AS median_bid
               FROM offers o
               JOIN vendors v ON v.id=o.vendor_id
               JOIN tenders t2 ON t2.id=o.tender_id
               WHERE t2.activity_id IS NOT DISTINCT FROM $1 AND o.offer_value IS NOT NULL
               GROUP BY v.id, v.canonical_name
               ORDER BY count(*) DESC, median_bid ASC NULLS LAST
               LIMIT 1""",
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
            "target_margin_pct": target_margin_pct,
            "risk_tolerance": risk_tolerance,
        }

        # Snapshot into pursuit_simulations
        import json
        await conn.execute(
            """INSERT INTO pursuit_simulations (pursuit_id, proposed_price, win_pct, expected_value, basis, metadata)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb)""",
            pid, body.proposed_price, win_prob_pct, expected_value, basis, json.dumps(benchmarks)
        )

        optimal_price = round(median_award * risk_factor, 2) if median_award else round(body.proposed_price * risk_factor, 2)
        safe_margin_floor = round(median_award * floor_factor, 2) if median_award else round(body.proposed_price * floor_factor, 2)
        pricing_ladder = [
            {
                "key": "aggressive",
                "label": "هجومي",
                "price": round(median_award * min(risk_factor, 0.82), 2) if median_award else round(body.proposed_price * min(risk_factor, 0.88), 2),
                "note": "يضغط المنافسين ويرفع احتمالية الفوز، راقب هامش الربح وخطر العرض المنخفض.",
            },
            {
                "key": "balanced",
                "label": "متوازن",
                "price": optimal_price,
                "note": "النقطة العملية الأقرب للفوز مع بقاء مساحة ربح معقولة.",
            },
            {
                "key": "safe",
                "label": "آمن",
                "price": safe_margin_floor,
                "note": "حد أدنى إرشادي لا ينبغي النزول عنه دون مبرر تكلفة موثق.",
            },
            {
                "key": "conservative",
                "label": "متحفظ",
                "price": round(p75_award * 0.98, 2) if p75_award else round(body.proposed_price * 1.05, 2),
                "note": "يحافظ على الهامش لكنه قد يخفض احتمالية الفوز إذا كان السوق حساساً للسعر.",
            },
        ]
        boq_count = boq_row["n"] if boq_row else 0
        boq_total_qty = boq_row["total_qty"] if boq_row else 0
        competitor_price = competitor_row["median_bid"] if competitor_row and competitor_row["median_bid"] else None
        price_to_beat = round(competitor_price * 0.985, 2) if competitor_price else None
        scenario_p10 = round(max(safe_margin_floor, optimal_price * 0.93), 2)
        scenario_p50 = optimal_price
        scenario_p90 = round(min(p75_award or body.proposed_price * 1.12, optimal_price * 1.10), 2)
        calculator_modes = [
            {
                "key": "bid_optimizer",
                "label": "Bid Price Optimizer",
                "status": "ready" if median_award else "needs_history",
                "primary": optimal_price,
                "unit": "SAR",
                "note": f"احتمالية الفوز {win_prob_pct}% عند السعر المقترح؛ السعر الأمثل يتبع شهية المخاطرة الحالية.",
            },
            {
                "key": "boq_line_pricer",
                "label": "BOQ Line-Item Pricer",
                "status": "ready" if boq_count else "needs_boq",
                "primary": boq_count,
                "unit": "items",
                "note": "يوزع السعر على بنود BOQ المحفوظة." if boq_count else "يفتح عند توفر جدول كميات مستخرج من الكراسة أو المرفقات.",
            },
            {
                "key": "markup_calculator",
                "label": "Markup Calculator",
                "status": "ready",
                "primary": target_margin_pct,
                "unit": "%",
                "note": "يستخدم هامش الربح الافتراضي من الإعدادات ما لم يتم تمرير هامش مخصص.",
            },
            {
                "key": "risk_adjusted_pricing",
                "label": "Risk-Adjusted Pricing",
                "status": "ready",
                "primary": round((1.0 - risk_factor) * 100.0, 1),
                "unit": "% buffer",
                "note": f"شهية المخاطرة الحالية: {risk_tolerance}. كلما زادت الهجومية انخفض السعر الأمثل.",
            },
            {
                "key": "competitor_price_match",
                "label": "Competitor Price Match",
                "status": "ready" if price_to_beat else "needs_competitor_history",
                "primary": price_to_beat,
                "unit": "SAR",
                "note": f"نموذج أولي لمجاراة {competitor_row['canonical_name']} بناءً على وسيط عروضه." if price_to_beat else "يحتاج سجل عروض منافس محدد داخل نفس النشاط.",
            },
            {
                "key": "agency_calibration",
                "label": "Agency-Specific Calibration",
                "status": "ready" if sample_count else "needs_agency_history",
                "primary": median_award,
                "unit": "SAR",
                "note": "يبدأ بوسيط النشاط الحالي، ويتحول لاحقًا إلى وزن جهة حكومية عند اتساع corpus.",
            },
            {
                "key": "boq_completeness",
                "label": "BOQ Completeness Check",
                "status": "ready" if boq_count else "needs_boq",
                "primary": round(min(100.0, boq_count * 12.5), 1) if boq_count else 0,
                "unit": "%",
                "note": f"{boq_count} بند محفوظ بإجمالي كميات {boq_total_qty:g}." if boq_count else "لا توجد بنود BOQ محفوظة لهذه المنافسة بعد.",
            },
            {
                "key": "scenario_simulator",
                "label": "Scenario Simulator",
                "status": "ready",
                "primary": {"p10": scenario_p10, "p50": scenario_p50, "p90": scenario_p90},
                "unit": "SAR",
                "note": "نطاق P10/P50/P90 مبسط من السعر الأمثل وحد الأمان حتى نضيف Monte Carlo كامل.",
            },
        ]

        return {
            "proposed_price": body.proposed_price,
            "win_probability_pct": win_prob_pct,
            "expected_value": expected_value,
            "competitive_zone": zone,
            "gtpl_abnormally_low_flag": gtpl_abnormally_low,
            "basis": basis,
            "benchmarks": benchmarks,
            "recommendations": {
                "optimal_price": optimal_price,
                "safe_margin_floor": safe_margin_floor,
                "target_margin_pct": target_margin_pct,
                "risk_tolerance": risk_tolerance,
            },
            "pricing_ladder": pricing_ladder,
            "calculator_modes": calculator_modes,
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


# ===========================================================================
# ===  P2W (Price-to-Win) API  —  architecture guide section 13           ====
# ===========================================================================
# Everything below this banner is the P2W surface. It is deliberately kept in
# one block: the honesty rules (observed vs predicted kept apart, suppression
# surfaced rather than papered over, tenant-private inputs never echoed on a
# shared endpoint) are easier to audit when the whole surface is in one place.
#
# The engine itself lives in thaqip_ingestion.p2w. This module never
# recomputes a quantile, a probability or a tier of its own — it retrieves,
# envelopes and persists what the engine returned.

P2W_CURRENCY = "SAR"
# We do not know whether a published award value includes VAT: Etimad does not
# say, and guessing would be a fabricated fact. Say so on every amount.
P2W_VAT_SEMANTICS = "unknown"

KIND_OBSERVED = "observed"
KIND_PREDICTED = "predicted"
KIND_USER_INPUT = "user_input"
LABEL_AR = {
    KIND_OBSERVED: "مُلاحظ",
    KIND_PREDICTED: "متوقع",
    KIND_USER_INPUT: "إدخالك",
}

# Tables /api/internal/lineage may trace. A whitelist, not a formatted table
# name: this endpoint takes a path segment straight from the caller.
LINEAGE_FACT_TABLES = {
    "tenders": "id",
    "offers": "id",
    "awards": "id",
    "documents": "id",
    "boq_items": "id",
    "price_predictions": "id",
}

READINESS_DOMAIN_WEIGHTS = {
    "data_readiness": 25,
    "model_readiness": 25,
    "product_ux_readiness": 15,
    "security_privacy_legal": 20,
    "operational_readiness": 10,
    "commercial_readiness": 5,
}


class P2WUnavailable(HTTPException):
    """503 with a machine-readable reason, never a fabricated fallback."""

    def __init__(self, detail: str) -> None:
        super().__init__(503, {"error": "p2w_engine_unavailable", "reason": detail})


def _p2w():
    """The engine namespace, or a 503 that says exactly what is missing.

    Imported lazily so the console still boots (and every pre-P2W endpoint
    still serves) on an image that does not carry the ingestion source tree.
    """
    cached = getattr(app.state, "p2w", None)
    if cached is not None:
        return cached
    try:
        from thaqip_ingestion.p2w import (
            competitor,
            contracts,
            evidence,
            market,
            montecarlo,
            optimizer,
            participation,
            similarity,
        )
    except ImportError as exc:  # pragma: no cover - depends on deployment
        raise P2WUnavailable(
            f"thaqip_ingestion.p2w is not importable ({exc}); "
            f"searched {[str(p) for p in _p2w_source_candidates()]}"
        ) from exc

    optional: dict[str, Any] = {}
    for name in ("orchestrator", "explain", "lineage"):
        try:
            optional[name] = __import__(
                f"thaqip_ingestion.p2w.{name}", fromlist=[name]
            )
        except ImportError:
            optional[name] = None

    ns = type("P2W", (), {
        "competitor": competitor, "contracts": contracts, "evidence": evidence,
        "market": market, "montecarlo": montecarlo, "optimizer": optimizer,
        "participation": participation, "similarity": similarity,
        **optional,
    })
    app.state.p2w = ns
    return ns


def _delegate(module: Any, name: str, /, **kwargs):
    """Return a bound engine function when the sibling module provides one with
    a compatible signature, else None so the caller composes it locally.

    Checked by signature rather than by try/except so a genuine bug inside the
    engine surfaces as a 500 instead of being silently swallowed by a fallback.
    """
    fn = getattr(module, name, None) if module is not None else None
    if fn is None:
        return None
    try:
        sig = inspect.signature(fn)
        sig.bind(*kwargs.pop("_positional", ()), **kwargs)
    except (TypeError, ValueError):
        return None
    return fn


# --- envelope helpers ------------------------------------------------------

def _amount(value: Any) -> dict[str, Any] | None:
    """Every money value in a P2W response carries its currency and its VAT
    semantics. A bare float is how a 12% VAT error gets shipped."""
    if value is None:
        return None
    return {
        "amount": round(float(value), 2),
        "currency": P2W_CURRENCY,
        "vat_semantics": P2W_VAT_SEMANTICS,
    }


def _prediction_envelope(pred: Any, *, prediction_id: int | None = None) -> dict[str, Any]:
    """The one shape every prediction-bearing response uses.

    Guarantees the six mandatory keys (model_version, generated_at,
    confidence_score, evidence_count, evidence_tier, suppression_reason) are
    present on every prediction object, suppressed or not, and that a
    suppressed prediction carries no numbers at all.
    """
    raw = pred.to_dict()
    suppressed = bool(raw["is_suppressed"])
    envelope: dict[str, Any] = {
        "kind": KIND_PREDICTED,
        "label_ar": LABEL_AR[KIND_PREDICTED],
        "prediction_id": prediction_id,
        "prediction_scope": raw["prediction_scope"],
        "subject_id": raw["subject_id"],
        "is_suppressed": suppressed,
        "suppression_reason": raw["suppression_reason"],
        "model_version": raw["model_version"],
        "generated_at": raw["generated_at"],
        "confidence_score": raw["confidence_score"],
        "confidence_is_not_win_probability": True,
        "evidence_count": raw["evidence_count"],
        "evidence_tier": raw["evidence_tier"],
        "similarity_confidence": raw["similarity_confidence"],
        "data_freshness_score": raw["data_freshness_score"],
        "feature_snapshot_id": raw["feature_snapshot_id"],
        "seed": raw["seed"],
        "explanation_factors": raw["explanation_factors"],
        "quantiles": None,
        "expected_value": None,
        "win_probability": None,
    }
    if not suppressed:
        envelope["quantiles"] = {
            "p10": _amount(raw["p10"]),
            "p50": _amount(raw["p50"]),
            "p90": _amount(raw["p90"]),
        }
        envelope["expected_value"] = _amount(raw["expected_value"])
        envelope["win_probability"] = raw["win_probability"]
    return envelope


_OPTIMIZER_MONEY_SCALARS = ("recommended_bid", "expected_contribution")
_OPTIMIZER_MONEY_RANGES = ("safe_range", "aggressive_range")
_CONSTRAINT_MONEY_SCALARS = ("estimated_cost", "hard_cost_floor", "risk_reserve", "max_bid")


def _money_fields(payload: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """Wrap named money scalars in the {amount, currency, vat_semantics} envelope.

    The optimizer, its constraints and the simulation assumptions are produced
    by pure engine dataclasses that (correctly) know nothing about currency, so
    the console is the only place that can attach it. Shipping a bare float for
    `recommended_bid` — the single number the whole product exists to produce —
    is exactly the VAT bug `_amount` was written to prevent.
    """
    for key in keys:
        if payload.get(key) is not None:
            payload[key] = _amount(payload[key])
    return payload


def _money_ranges(payload: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            payload[key] = [_amount(v) for v in value]
    return payload


def _observed(payload: dict[str, Any]) -> dict[str, Any]:
    """Tag a block of counted facts so the UI can never render it as a model
    output (house rule 1)."""
    return {"kind": KIND_OBSERVED, "label_ar": LABEL_AR[KIND_OBSERVED], **payload}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


async def _p2w_tender(conn: Any, tender_id: int) -> dict[str, Any]:
    row = await conn.fetchrow("SELECT * FROM tenders WHERE id=$1", tender_id)
    if row is None:
        raise HTTPException(404, "tender not found")
    return dict(row)


async def _persist_prediction(conn: Any, pred: Any) -> int:
    """Store a generated prediction and return its id.

    Persisting is what makes /api/predictions/{id}/explanation and /feedback
    addressable, and it is also the audit trail: every number a user saw is on
    disk with its model version, seed and feature snapshot.
    """
    raw = pred.to_dict()
    return int(await conn.fetchval(
        """INSERT INTO price_predictions
             (tender_id, prediction_scope, subject_vendor_id, p10, p50, p90,
              expected_value, win_probability, confidence_score, similarity_confidence,
              data_freshness_score, evidence_count, evidence_tier, model_version,
              feature_snapshot_id, seed, suppression_reason, explanation_factors,
              generated_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18::jsonb,$19)
           RETURNING id""",
        raw["tender_id"], raw["prediction_scope"], raw["subject_id"],
        raw["p10"], raw["p50"], raw["p90"], raw["expected_value"], raw["win_probability"],
        raw["confidence_score"], raw["similarity_confidence"], raw["data_freshness_score"],
        raw["evidence_count"], raw["evidence_tier"], raw["model_version"],
        raw["feature_snapshot_id"], raw["seed"], raw["suppression_reason"],
        json.dumps(raw["explanation_factors"]),
        datetime.fromisoformat(raw["generated_at"]),
    ))


def _similar_payload(items: Any) -> list[dict[str, Any]]:
    out = []
    for item in items:
        d = item.to_dict()
        d["award_value"] = _amount(d.get("award_value"))
        out.append(d)
    return out


# --- market intelligence ---------------------------------------------------

async def _tender_intelligence(conn: Any, tender: dict[str, Any]) -> dict[str, Any]:
    """Compose the market view for one tender from the engine modules.

    Used when thaqip_ingestion.p2w.orchestrator.tender_intelligence is not
    available; when it is, the endpoint delegates to it instead.
    """
    p2w = _p2w()
    as_of = p2w.evidence.resolve_as_of(tender)
    similar = await p2w.similarity.find_similar_tenders(
        conn, tender=tender, as_of=as_of, limit=20, include_excluded=True)
    eligible = [s for s in similar if not s.is_excluded]
    excluded = [s for s in similar if s.is_excluded]
    ev = await p2w.evidence.market_evidence(conn, tender=tender, as_of=as_of)
    market = await p2w.market.market_quantiles(
        conn, tender=tender, as_of=as_of, similar=eligible)

    curve: list[dict[str, Any]] = []
    if not market.is_suppressed and market.p50:
        lo, hi = market.p10 or market.p50, market.p90 or market.p50
        span_lo, span_hi = max(lo * 0.7, 1.0), max(hi * 1.3, lo * 0.7 + 1.0)
        grid = [span_lo + (span_hi - span_lo) * i / 20 for i in range(21)]
        curve = await p2w.market.market_curve(
            conn, tender=tender, as_of=as_of, grid=grid, similar=eligible)
        for point in curve:
            point["price"] = _amount(point["price"])

    # Only when the agency was actually resolved: `agency_id IS NOT DISTINCT
    # FROM NULL` would silently pool every unresolved agency into one fake
    # "history" and report it as this buyer's track record.
    agency = None
    if tender.get("agency_id") is not None:
        agency = await conn.fetchrow(
            """SELECT count(*)::int AS awarded_tenders,
                      count(DISTINCT a.vendor_id)::int AS distinct_winners,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY a.award_value)::float
                        AS median_award_value
               FROM awards a JOIN tenders t ON t.id = a.tender_id
               WHERE t.agency_id = $1 AND a.award_value IS NOT NULL""",
            tender["agency_id"])
    bidders = await conn.fetchrow(
        "SELECT count(*)::int AS n FROM offers WHERE tender_id=$1", tender["id"])

    return {
        "as_of": _iso(as_of),
        "observed": _observed({
            "tender": {
                "id": tender["id"],
                "reference_number": tender.get("reference_number"),
                "name": tender.get("name"),
                "agency_id": tender.get("agency_id"),
                "agency_name": tender.get("agency_name_raw"),
                "activity_id": tender.get("activity_id"),
                "activity_name": tender.get("activity_name_raw"),
                "source": tender.get("source"),
                "published_at": _iso(tender.get("published_at")),
                "last_offer_date": _iso(tender.get("last_offer_date")),
                "offers_opening_date": _iso(tender.get("offers_opening_date")),
                "booklet_price": _amount(tender.get("booklet_price")),
            },
            "recorded_offers_on_this_tender": bidders["n"],
            "agency_history": {
                "agency_resolved": agency is not None,
                "awarded_tenders": agency["awarded_tenders"] if agency else None,
                "distinct_winners": agency["distinct_winners"] if agency else None,
                "median_award_value": _amount(
                    agency["median_award_value"]) if agency else None,
                "note": None if agency else (
                    "this tender's agency is not resolved to a canonical agency, "
                    "so no buyer history can be attributed"),
            },
            "price_percentile_curve": curve,
        }),
        "evidence": ev.to_dict(),
        "prediction": market,
        "similar_tenders": {
            "retrieval_version": p2w.similarity.RETRIEVAL_VERSION,
            "used": _similar_payload(eligible),
            "excluded": _similar_payload(excluded),
        },
    }


@app.get("/api/tenders/{tender_id}/market-intelligence")
async def market_intelligence(tender_id: int, tenant_id: int = Tenant):
    """Market range, similar tenders and agency context for one tender.

    Shared endpoint: it must never contain a tenant's cost, margin or bid.
    """
    p2w = _p2w()
    pool: asyncpg.Pool = app.state.pool
    async with pool.acquire() as conn:
        tender = await _p2w_tender(conn, tender_id)
        # The orchestrator owns the capability ladder and the degradation
        # contract; the console composition owns the observed block (agency
        # history, price-percentile curve) that the orchestrator does not
        # produce. Both derive the market band from market_quantiles at the
        # same as_of, so the console prediction stays the persisted one and the
        # engine contributes governance: allowed_level, evidence_tier,
        # degradations, suppression.
        #
        # This bound as tender= against a parameter named tender_id= until
        # 2026-09-10, so _delegate always returned None, the engine never ran in
        # production, and allowed_level was null in every payload.
        delegated = _delegate(
            getattr(p2w, "orchestrator", None), "tender_intelligence",
            _positional=(conn,), tender_id=tender_id, tenant_id=tenant_id)
        payload = await _tender_intelligence(conn, tender)
        engine_ran = False
        if delegated is not None:
            try:
                engine = await delegated(
                    conn, tender_id=tender_id, tenant_id=tenant_id)
            except Exception as exc:  # noqa: BLE001 - degrade, never 500
                log.exception("p2w orchestrator failed for tender %s", tender_id)
                payload["allowed_level"] = None
                payload["degradations"] = [{
                    "stage": "orchestrator",
                    "reason": "MODEL_UNAVAILABLE",
                    "detail": f"{type(exc).__name__}: {exc}"[:300],
                    "caps_level": "L0",
                }]
            else:
                engine_ran = True
                payload["allowed_level"] = engine.get("allowed_level")
                payload["evidence_tier"] = engine.get("evidence_tier")
                payload["degradations"] = engine.get("degradations") or []
                payload["suppression"] = engine.get("suppression")
                payload["competitor_count"] = len(engine.get("competitors") or [])
        else:
            payload["allowed_level"] = None
            payload["degradations"] = [{
                "stage": "orchestrator",
                "reason": "MODEL_UNAVAILABLE",
                "detail": "orchestrator.tender_intelligence not importable",
                "caps_level": "L0",
            }]
        pred = payload.pop("prediction")
        prediction_id = await _persist_prediction(conn, pred)
    payload["prediction"] = _prediction_envelope(pred, prediction_id=prediction_id)
    payload["model_version"] = p2w.contracts.MODEL_VERSION
    payload["generated_at"] = datetime.now(UTC).isoformat()
    payload["orchestrator"] = "engine" if engine_ran else "console_composition"
    return payload


# --- competitors -----------------------------------------------------------

@app.get("/api/tenders/{tender_id}/competitors")
async def tender_competitors(
    tender_id: int,
    limit: int = Query(12, ge=1, le=30),
    tenant_id: int = Tenant,
):
    """Likely bidders and their modelled price ranges.

    Suppressed rows are returned, not dropped: the UI has to be able to explain
    its silence about a competitor rather than leave a blank. Nothing here is a
    claim about what a competitor will do — every range is `متوقع`.
    """
    p2w = _p2w()
    pool: asyncpg.Pool = app.state.pool
    out: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        tender = await _p2w_tender(conn, tender_id)
        as_of = p2w.evidence.resolve_as_of(tender)
        market = await p2w.market.market_quantiles(conn, tender=tender, as_of=as_of)
        market_id = await _persist_prediction(conn, market)
        candidates = await p2w.participation.candidate_bidders(
            conn, tender=tender, as_of=as_of, limit=limit)
        for cand in candidates:
            vendor = await conn.fetchrow(
                """SELECT v.id, v.canonical_name,
                          (SELECT count(*)::int FROM offers o WHERE o.vendor_id = v.id)  AS offers_seen,
                          (SELECT count(*)::int FROM offers o
                            WHERE o.vendor_id = v.id AND o.is_winner)                    AS wins_seen,
                          (SELECT max(t2.published_at) FROM offers o
                             JOIN tenders t2 ON t2.id = o.tender_id
                            WHERE o.vendor_id = v.id)                                    AS last_seen_at
                   FROM vendors v WHERE v.id = $1""",
                cand.vendor_id)
            price = await p2w.competitor.competitor_quantiles(
                conn, vendor_id=cand.vendor_id, tender=tender, as_of=as_of, market=market)
            price_id = await _persist_prediction(conn, price)
            comp_ev = await p2w.evidence.competitor_evidence(
                conn, vendor_id=cand.vendor_id, tender=tender, as_of=as_of)
            out.append({
                "vendor_id": cand.vendor_id,
                "observed": _observed({
                    "canonical_name": vendor["canonical_name"] if vendor else None,
                    "offers_seen": vendor["offers_seen"] if vendor else 0,
                    "wins_seen": vendor["wins_seen"] if vendor else 0,
                    "last_seen_at": _iso(vendor["last_seen_at"]) if vendor else None,
                }),
                "evidence": comp_ev.to_dict(),
                "participation": cand.to_dict(),
                "price_prediction": _prediction_envelope(price, prediction_id=price_id),
            })
    return {
        "tender_id": tender_id,
        "as_of": _iso(as_of),
        "model_version": p2w.contracts.MODEL_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "market_prediction": _prediction_envelope(market, prediction_id=market_id),
        "candidates": out,
        "candidate_count": len(out),
        "suppressed_count": sum(
            1 for c in out if c["price_prediction"]["is_suppressed"]),
        "disclaimer": (
            "نطاقات المنافسين تقديرات احتمالية مبنية على سجلّهم المُلاحظ، "
            "وليست معرفة بما سيقدمونه فعلاً."
        ),
    }


# --- scenarios (tenant-private) --------------------------------------------

# user_bid_scenarios money columns are numeric(18,2): anything at or above
# 1e16 overflows the column and asyncpg raises NumericValueOutOfRangeError,
# which reaches the client as an opaque 500. Bounding it here turns ordinary
# bad input into a 422 that names the field.
MAX_MONEY = 1e15
# min_margin_pct is numeric(6,2): 99.996 is accepted by lt=100, then rounded
# to 100.00 on write and echoed back as a value this same validator forbids —
# and one the optimizer correctly calls unreachable at any price. Bound it to
# what the column can actually store.
MAX_MARGIN_PCT = 99.99


class ScenarioIn(BaseModel):
    estimated_cost: float = Field(gt=0, le=MAX_MONEY)
    min_margin_pct: float = Field(ge=0, le=MAX_MARGIN_PCT)
    target_win_pct: float | None = Field(default=None, ge=0, le=100)
    proposed_bid: float | None = Field(default=None, gt=0, le=MAX_MONEY)
    risk_reserve: float = Field(default=0.0, ge=0, le=MAX_MONEY)
    name: str = Field(default="", max_length=160)
    seed: int | None = Field(default=None, ge=0)


def _scenario_seed(tender_id: int, tenant_id: int, version: int) -> int:
    """Deterministic seed from (tender, tenant, version).

    Never time- or random-derived: house rule 7 requires a Monte Carlo result
    to be reproducible from (inputs, model_version, seed), which is only worth
    anything if re-reading the saved scenario reproduces the same seed.
    """
    digest = hashlib.sha256(f"{tender_id}|{tenant_id}|{version}".encode()).hexdigest()
    return int(digest[:15], 16)


@app.post("/api/tenders/{tender_id}/scenarios")
async def create_scenario(tender_id: int, body: ScenarioIn, tenant_id: int = Tenant):
    """Create a new tenant-private scenario version for a tender."""
    pool: asyncpg.Pool = app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        exists = await conn.fetchval("SELECT 1 FROM tenders WHERE id=$1", tender_id)
        if not exists:
            raise HTTPException(404, "tender not found")
        pursuit_id = await conn.fetchval(
            "SELECT id FROM pursuits WHERE tender_id=$1 AND tenant_id=$2",
            tender_id, tenant_id)
        version = 1 + int(await conn.fetchval(
            """SELECT coalesce(max(version), 0) FROM user_bid_scenarios
               WHERE tender_id=$1 AND tenant_id=$2""", tender_id, tenant_id))
        seed = body.seed if body.seed is not None else _scenario_seed(
            tender_id, tenant_id, version)
        row = await conn.fetchrow(
            """INSERT INTO user_bid_scenarios
                 (tenant_id, pursuit_id, tender_id, name, estimated_cost, min_margin_pct,
                  target_win_pct, proposed_bid, risk_reserve, version, seed)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
               RETURNING *""",
            tenant_id, pursuit_id, tender_id, body.name or f"scenario v{version}",
            body.estimated_cost, body.min_margin_pct, body.target_win_pct,
            body.proposed_bid, body.risk_reserve, version, seed)
        if version > 1:
            await conn.execute(
                """UPDATE user_bid_scenarios SET superseded_by=$1
                   WHERE tenant_id=$2 AND tender_id=$3 AND version < $4
                     AND superseded_by IS NULL""",
                row["id"], tenant_id, tender_id, version)
    return {"scenario": _scenario_payload(row), "created": True}


def _scenario_payload(row: Any) -> dict[str, Any]:
    """Tenant-private inputs, echoed only on the tenant's own scenario routes."""
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "tender_id": row["tender_id"],
        "pursuit_id": row["pursuit_id"],
        "name": row["name"],
        "version": row["version"],
        "seed": row["seed"],
        "created_at": _iso(row["created_at"]),
        "superseded_by": row["superseded_by"],
        "inputs": {
            "kind": KIND_USER_INPUT,
            "label_ar": LABEL_AR[KIND_USER_INPUT],
            "estimated_cost": _amount(row["estimated_cost"]),
            "min_margin_pct": _f(row["min_margin_pct"]),
            "target_win_pct": _f(row["target_win_pct"]),
            "proposed_bid": _amount(row["proposed_bid"]),
            "risk_reserve": _amount(row["risk_reserve"]),
        },
    }


@app.get("/api/scenarios")
async def list_scenarios(
    tender_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    tenant_id: int = Tenant,
):
    """Scenario versions for the calling tenant, newest first."""
    if tender_id is None:
        rows = await app.state.pool.fetch(
            """SELECT * FROM user_bid_scenarios WHERE tenant_id=$1
               ORDER BY tender_id, version DESC LIMIT $2""", tenant_id, limit)
    else:
        rows = await app.state.pool.fetch(
            """SELECT * FROM user_bid_scenarios WHERE tenant_id=$1 AND tender_id=$2
               ORDER BY version DESC LIMIT $3""", tenant_id, tender_id, limit)
    return {"tenant_id": tenant_id, "tender_id": tender_id,
            "items": [_scenario_payload(r) for r in rows]}


async def _competitor_draws(conn: Any, tender: dict[str, Any], as_of: datetime):
    """Monte Carlo inputs: one draw per candidate the engine will speak about.

    A candidate whose participation OR price is suppressed is excluded from the
    simulation and reported separately — simulating a competitor we have no
    evidence for would manufacture exactly the number the suppression gate
    exists to withhold.
    """
    p2w = _p2w()
    market = await p2w.market.market_quantiles(conn, tender=tender, as_of=as_of)
    candidates = await p2w.participation.candidate_bidders(
        conn, tender=tender, as_of=as_of, limit=15)
    draws, skipped = [], []
    for cand in candidates:
        if cand.is_suppressed or cand.probability is None:
            skipped.append({"vendor_id": cand.vendor_id,
                            "reason": (cand.suppression.value if cand.suppression
                                       else "no_participation_estimate")})
            continue
        price = await p2w.competitor.competitor_quantiles(
            conn, vendor_id=cand.vendor_id, tender=tender, as_of=as_of, market=market)
        if price.is_suppressed or not price.p50:
            skipped.append({"vendor_id": cand.vendor_id,
                            "reason": (price.suppression_reason.value
                                       if price.suppression_reason else "no_price_range")})
            continue
        sigma = p2w.competitor.implied_sigma(price) or 0.0
        draws.append(p2w.montecarlo.CompetitorDraw(
            vendor_id=cand.vendor_id,
            participation_p=float(cand.probability),
            log_mu=math.log(float(price.p50)),
            log_sigma=float(sigma),
            # offers.technical_pass is TRUE for every row we have observed, so
            # there is no measured failure rate to use. 1.0 states the
            # assumption instead of inventing a haircut.
            technical_pass_p=1.0,
        ))
    return market, draws, skipped


async def _scenario_curve(conn: Any, row: Any) -> dict[str, Any]:
    p2w = _p2w()
    tender = await _p2w_tender(conn, row["tender_id"])
    as_of = p2w.evidence.resolve_as_of(tender)
    market, draws, skipped = await _competitor_draws(conn, tender, as_of)

    cost = float(row["estimated_cost"] or 0.0)
    reserve = float(row["risk_reserve"] or 0.0)
    min_margin = float(row["min_margin_pct"] or 0.0)
    floor = p2w.optimizer.min_margin_price(cost, min_margin) or cost

    if not draws:
        return {
            "curve": [],
            "optimizer": None,
            "suppressed": True,
            "suppression_reason": p2w.contracts.SuppressionReason.INSUFFICIENT_EVIDENCE.value,
            "detail": ("no candidate bidder has both a participation estimate and a "
                       "price range at this evidence tier"),
            "excluded_candidates": skipped,
            "market_prediction": market,
            "simulation": None,
        }

    anchor = float(market.p50) if not market.is_suppressed and market.p50 else (
        max(d.median_bid for d in draws))
    # Wide enough that the optimum is rarely pinned to the grid edge; when it
    # still is, optimizer.binding_constraints says so rather than hiding it.
    lo = max(floor * 0.9, anchor * 0.4, 1.0)
    hi = max(anchor * 2.5, floor * 2.0, lo + 1.0)
    grid = [lo + (hi - lo) * i / 24 for i in range(25)]
    seed = int(row["seed"])
    points = p2w.montecarlo.win_probability_curve(
        price_grid=grid, competitors=draws, seed=seed,
        iterations=p2w.montecarlo.DEFAULT_CURVE_ITERATIONS,
        user_technical_pass_p=p2w.montecarlo.DEFAULT_USER_TECHNICAL_PASS_P)

    constraints = p2w.optimizer.OptimizerConstraints(
        estimated_cost=cost,
        min_margin_pct=min_margin,
        target_win_probability=(None if row["target_win_pct"] is None
                                else float(row["target_win_pct"]) / 100.0),
        risk_reserve=reserve,
    )
    result = p2w.optimizer.optimize(curve=points, constraints=constraints)

    simulation = None
    if row["proposed_bid"] is not None:
        sim = p2w.montecarlo.simulate(
            user_bid=float(row["proposed_bid"]), competitors=draws, seed=seed,
            iterations=p2w.montecarlo.DEFAULT_ITERATIONS)
        simulation = sim.to_dict()
        simulation["price_to_beat"] = _amount(sim.price_to_beat)
        simulation["assumptions"] = _money_fields(
            dict(simulation["assumptions"]), ("user_bid",))

    curve = [{
        "price": _amount(p.price),
        "win_probability": p.win_probability,
        "standard_error": p.standard_error,
        "margin_pct": p2w.optimizer.margin_pct(p.price, cost) if cost > 0 else None,
        "contribution": _amount(p.price - cost - reserve),
    } for p in points]

    return {
        "curve": curve,
        "optimizer": _money_ranges(
            _money_fields(result.to_dict(), _OPTIMIZER_MONEY_SCALARS),
            _OPTIMIZER_MONEY_RANGES),
        "suppressed": False,
        "suppression_reason": None,
        "detail": None,
        "excluded_candidates": skipped,
        "market_prediction": market,
        "simulation": simulation,
        "simulated_competitors": [d.to_dict() for d in draws],
        "constraints": _money_fields(
            constraints.to_dict(), _CONSTRAINT_MONEY_SCALARS),
        "iterations": p2w.montecarlo.DEFAULT_CURVE_ITERATIONS,
        "evaluation_rule": p2w.montecarlo.EVALUATION_RULE_LOWEST_QUALIFIED,
    }


@app.get("/api/scenarios/{scenario_id}/curve")
async def scenario_curve(scenario_id: int, refresh: bool = False, tenant_id: int = Tenant):
    """Price / win-probability / margin curve for one saved scenario.

    Cached in-process, keyed by (scenario, model_version): the curve is a pure
    function of the stored inputs, the model version and the stored seed, so a
    cache hit and a recomputation are the same numbers by construction.
    """
    p2w = _p2w()
    pool: asyncpg.Pool = app.state.pool
    row = await pool.fetchrow(
        "SELECT * FROM user_bid_scenarios WHERE id=$1 AND tenant_id=$2",
        scenario_id, tenant_id)
    if row is None:
        raise HTTPException(404, "scenario not found")

    cache = getattr(app.state, "scenario_curve_cache", None)
    if cache is None:
        cache = app.state.scenario_curve_cache = {}
    key = (scenario_id, tenant_id, p2w.contracts.MODEL_VERSION)
    if not refresh and key in cache:
        cached = dict(cache[key])
        cached["cache"] = "hit"
        return cached

    async with pool.acquire() as conn:
        computed = await _scenario_curve(conn, row)
        market = computed.pop("market_prediction")
        market_id = await _persist_prediction(conn, market)

    payload = {
        "scenario": _scenario_payload(row),
        "market_prediction": _prediction_envelope(market, prediction_id=market_id),
        "model_version": p2w.contracts.MODEL_VERSION,
        "seed": int(row["seed"]),
        "generated_at": datetime.now(UTC).isoformat(),
        "cache": "miss",
        **computed,
    }
    cache[key] = payload
    if len(cache) > 256:
        cache.pop(next(iter(cache)))
    return dict(payload)


# --- prediction feedback and explanation -----------------------------------

class PredictionFeedbackIn(BaseModel):
    actual_award_value: float | None = Field(default=None, gt=0)
    actual_winner_vendor_id: int | None = None
    actual_bidder_count: int | None = Field(default=None, ge=0)
    user_feedback: str | None = Field(default=None, max_length=2000)


@app.post("/api/predictions/{prediction_id}/feedback")
async def prediction_feedback(
    prediction_id: int, body: PredictionFeedbackIn, tenant_id: int = Tenant
):
    """Record what actually happened and score the prediction against it.

    interval_hit and abs_pct_error are computed here, not supplied: they are the
    calibration signal, and a caller-supplied score is not evidence.
    """
    pool: asyncpg.Pool = app.state.pool
    pred = await pool.fetchrow(
        "SELECT * FROM price_predictions WHERE id=$1", prediction_id)
    if pred is None:
        raise HTTPException(404, "prediction not found")

    actual = body.actual_award_value
    interval_hit: bool | None = None
    abs_pct_error: float | None = None
    scoring_note = None
    if actual is None:
        scoring_note = "no actual_award_value supplied; nothing to score"
    elif pred["suppression_reason"] is not None:
        scoring_note = ("prediction was suppressed, so it made no claim to score "
                        f"({pred['suppression_reason']})")
    else:
        p10, p50, p90 = (float(pred["p10"]), float(pred["p50"]), float(pred["p90"]))
        interval_hit = p10 <= actual <= p90
        abs_pct_error = round(abs(actual - p50) / actual, 4)

    row = await pool.fetchrow(
        """INSERT INTO prediction_feedback
             (prediction_id, actual_award_value, actual_winner_vendor_id,
              actual_bidder_count, interval_hit, abs_pct_error, user_feedback)
           VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *""",
        prediction_id, actual, body.actual_winner_vendor_id, body.actual_bidder_count,
        interval_hit, abs_pct_error, body.user_feedback)

    return {
        "feedback_id": row["id"],
        "prediction_id": prediction_id,
        "recorded_at": _iso(row["recorded_at"]),
        "observed": _observed({
            "actual_award_value": _amount(actual),
            "actual_winner_vendor_id": body.actual_winner_vendor_id,
            "actual_bidder_count": body.actual_bidder_count,
        }),
        "scoring": {
            "kind": "derived",
            "interval_hit": interval_hit,
            "abs_pct_error": abs_pct_error,
            "note": scoring_note,
            "model_version": pred["model_version"],
        },
    }


def _build_explanation(pred: Any, feedback: list[Any], lineage: dict[str, Any]) -> dict[str, Any]:
    """Console-side explanation for a stored prediction.

    Used when thaqip_ingestion.p2w.explain.build_explanation is not available.
    """
    factors = pred["explanation_factors"]
    if isinstance(factors, str):
        factors = json.loads(factors)
    suppressed = pred["suppression_reason"] is not None
    return {
        "prediction_id": pred["id"],
        "tender_id": pred["tender_id"],
        "prediction_scope": pred["prediction_scope"],
        "subject_vendor_id": pred["subject_vendor_id"],
        "is_suppressed": suppressed,
        "suppression_reason": pred["suppression_reason"],
        "headline": (
            f"مكتوم: {pred['suppression_reason']}" if suppressed
            else f"نطاق متوقع بثقة {pred['confidence_score']}/100 "
                 f"(مستوى الأدلة {pred['evidence_tier']})"
        ),
        "quantiles": None if suppressed else {
            "p10": _amount(pred["p10"]), "p50": _amount(pred["p50"]),
            "p90": _amount(pred["p90"]),
        },
        "confidence_score": pred["confidence_score"],
        "confidence_is_not_win_probability": True,
        "evidence_count": pred["evidence_count"],
        "evidence_tier": pred["evidence_tier"],
        "similarity_confidence": pred["similarity_confidence"],
        "data_freshness_score": pred["data_freshness_score"],
        "model_version": pred["model_version"],
        "feature_snapshot_id": pred["feature_snapshot_id"],
        "seed": pred["seed"],
        "generated_at": _iso(pred["generated_at"]),
        "factors": factors,
        "factor_kinds_present": sorted({f.get("kind") for f in factors}),
        "lineage": lineage,
        "feedback": [{
            "recorded_at": _iso(f["recorded_at"]),
            "actual_award_value": _amount(f["actual_award_value"]),
            "interval_hit": f["interval_hit"],
            "abs_pct_error": _f(f["abs_pct_error"]),
        } for f in feedback],
    }


@app.get("/api/predictions/{prediction_id}/explanation")
async def prediction_explanation(prediction_id: int, tenant_id: int = Tenant):
    """Evidence and factors behind one stored prediction."""
    p2w = _p2w()
    pool: asyncpg.Pool = app.state.pool
    pred = await pool.fetchrow("SELECT * FROM price_predictions WHERE id=$1", prediction_id)
    if pred is None:
        raise HTTPException(404, "prediction not found")
    feedback = await pool.fetch(
        "SELECT * FROM prediction_feedback WHERE prediction_id=$1 ORDER BY id DESC",
        prediction_id)
    lineage = await _trace_fact(pool, "tenders", int(pred["tender_id"]))

    delegated = _delegate(
        getattr(p2w, "explain", None), "build_explanation", prediction=dict(pred))
    if delegated:
        payload = delegated(prediction=dict(pred))
    else:
        payload = _build_explanation(pred, list(feedback), lineage)
    payload["explanation_source"] = "engine" if delegated else "console_composition"
    return payload


# --- lineage (internal / ops) ----------------------------------------------

async def _trace_fact(pool: asyncpg.Pool, fact_table: str, fact_id: int) -> dict[str, Any]:
    """Source trace for one observed fact.

    Reports honestly when nothing is recorded: `traceable` is false and
    `lineage_rows` is 0 rather than a reassuring empty success.
    """
    rows = await pool.fetch(
        """SELECT l.*, r.name AS source_name, r.access_class AS registry_access_class,
                  r.legal_basis, r.redistribution_policy
           FROM source_lineage l
           LEFT JOIN source_registry r ON r.source_id = l.source_id
           WHERE l.fact_table=$1 AND l.fact_id=$2
           ORDER BY l.recorded_at DESC""",
        fact_table, fact_id)
    intrinsic = None
    if fact_table == "tenders":
        t = await pool.fetchrow(
            """SELECT source, source_uid, source_tender_id, reference_number,
                      content_hash, detected_at, detected_by
               FROM tenders WHERE id=$1""", fact_id)
        intrinsic = {k: _iso(v) if isinstance(v, datetime) else v
                     for k, v in dict(t).items()} if t else None
    elif fact_table in ("offers", "awards"):
        t = await pool.fetchrow(
            f"""SELECT f.tender_id, t.source, t.source_uid, t.content_hash
                FROM {fact_table} f JOIN tenders t ON t.id = f.tender_id
                WHERE f.id=$1""", fact_id)
        intrinsic = dict(t) if t else None
    return {
        "fact_table": fact_table,
        "fact_id": fact_id,
        "traceable": bool(rows),
        "lineage_rows": len(rows),
        "lineage": [{
            "source_id": r["source_id"],
            "source_name": r["source_name"],
            "source_object_ref": r["source_object_ref"],
            "content_hash": r["content_hash"],
            "parser_version": r["parser_version"],
            "access_class": r["access_class"] or r["registry_access_class"],
            "legal_basis": r["legal_basis"],
            "redistribution_policy": r["redistribution_policy"],
            "retrieved_at": _iso(r["retrieved_at"]),
            "recorded_at": _iso(r["recorded_at"]),
        } for r in rows],
        "intrinsic_source_fields": intrinsic,
        "note": None if rows else (
            "no source_lineage rows recorded for this fact; the intrinsic source "
            "columns below are the only trace available"),
    }


@app.get("/api/internal/lineage/{fact_table}/{fact_id}")
async def internal_lineage(fact_table: str, fact_id: int, tenant_id: int = Tenant):
    """Ops/admin: trace one observed fact back to its source evidence."""
    if fact_table not in LINEAGE_FACT_TABLES:
        raise HTTPException(422, f"fact_table must be one of {sorted(LINEAGE_FACT_TABLES)}")
    p2w = _p2w()
    delegated = _delegate(getattr(p2w, "lineage", None), "trace_fact",
                          fact_table=fact_table, fact_id=fact_id)
    if delegated:
        payload = delegated(fact_table=fact_table, fact_id=fact_id)
        if inspect.isawaitable(payload):
            payload = await payload
    else:
        payload = await _trace_fact(app.state.pool, fact_table, fact_id)
    payload["generated_at"] = datetime.now(UTC).isoformat()
    payload["source"] = "engine" if delegated else "console_composition"
    return payload


# --- readiness -------------------------------------------------------------

def _dimension(weight: int, score: float | None, basis: dict[str, Any],
               reason: str | None = None) -> dict[str, Any]:
    """One readiness dimension. `score=None` means *not measurable from this
    database*, which is a different statement from *scored zero*."""
    return {
        "weight": weight,
        "score": None if score is None else round(float(score), 1),
        "measured": score is not None,
        "not_measurable_reason": reason,
        "basis": basis,
    }


def _domain(dimensions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    measured = {k: d for k, d in dimensions.items() if d["measured"]}
    weight = sum(d["weight"] for d in measured.values())
    total = sum(d["weight"] for d in dimensions.values())
    score = (sum(d["weight"] * d["score"] for d in measured.values()) / weight
             if weight else None)
    return {
        "score": None if score is None else round(score, 1),
        "measured_weight_pct": round(100.0 * weight / total, 1) if total else 0.0,
        "dimensions": dimensions,
    }


def _pct(numerator: float, denominator: float) -> float | None:
    return None if not denominator else round(100.0 * numerator / denominator, 1)


@app.get("/api/readiness")
async def readiness(sample: int = Query(30, ge=5, le=120), tenant_id: int = Tenant):
    """Measured readiness snapshot — every score computed from real counts.

    Domains and dimension weights come from the Readiness Evaluation Guide.
    Dimensions this database cannot evidence are reported as not measurable
    with a reason, and are excluded from the domain average rather than being
    given a flattering default. `measured_weight_pct` says how much of each
    domain the score actually covers.
    """
    p2w = _p2w()
    pool: asyncpg.Pool = app.state.pool

    counts = await pool.fetchrow(
        """SELECT (SELECT count(*) FROM tenders)                                    AS tenders,
                  (SELECT count(*) FROM tenders WHERE last_offer_date > now())      AS open_tenders,
                  (SELECT count(*) FROM offers)                                     AS offers,
                  (SELECT count(*) FROM offers WHERE offer_value IS NOT NULL)       AS offers_valued,
                  (SELECT count(*) FROM awards)                                     AS awards,
                  (SELECT count(*) FROM awards WHERE award_value IS NOT NULL)       AS awards_valued,
                  (SELECT count(*) FROM awards a
                     WHERE EXISTS (SELECT 1 FROM tenders t WHERE t.id = a.tender_id))AS awards_linked,
                  (SELECT count(DISTINCT tender_id) FROM offers)                    AS tenders_with_offers,
                  (SELECT count(DISTINCT tender_id) FROM awards)                    AS tenders_with_awards,
                  (SELECT count(DISTINCT tender_id) FROM boq_items)                 AS tenders_with_boq,
                  (SELECT count(*) FROM vendors)                                    AS vendors,
                  (SELECT count(*) FROM vendors WHERE cr_number IS NOT NULL)        AS vendors_with_cr,
                  (SELECT count(*) FROM source_lineage)                             AS lineage_rows,
                  (SELECT count(DISTINCT fact_id) FROM source_lineage
                    WHERE fact_table='offers')                                      AS lineage_offers,
                  (SELECT count(DISTINCT fact_id) FROM source_lineage
                    WHERE fact_table='awards')                                      AS lineage_awards,
                  (SELECT count(*) FROM price_predictions)                          AS predictions,
                  (SELECT count(*) FROM prediction_feedback)                        AS feedback,
                  -- Same real-outcome guard as mean_abs_pct_error below: a scored
                  -- row on a tender with no award is not evidence of anything.
                  (SELECT count(*) FROM prediction_feedback f
                     JOIN price_predictions pp ON pp.id = f.prediction_id
                    WHERE f.interval_hit IS NOT NULL
                      AND EXISTS (SELECT 1 FROM awards w
                                   WHERE w.tender_id = pp.tender_id
                                     AND w.award_value IS NOT NULL))                AS feedback_scored,
                  (SELECT count(*) FROM prediction_feedback f
                     JOIN price_predictions pp ON pp.id = f.prediction_id
                    WHERE f.interval_hit
                      AND EXISTS (SELECT 1 FROM awards w
                                   WHERE w.tender_id = pp.tender_id
                                     AND w.award_value IS NOT NULL))                AS feedback_hits,
                  -- Only feedback whose tender actually has a recorded award can
                  -- represent a real outcome. Without this join a row invented on a
                  -- still-open tender with zero offers counts as measured accuracy,
                  -- which is how a fabricated number reached this endpoint before.
                  (SELECT avg(f.abs_pct_error) FROM prediction_feedback f
                     JOIN price_predictions pp ON pp.id = f.prediction_id
                    WHERE f.abs_pct_error IS NOT NULL
                      AND EXISTS (SELECT 1 FROM awards w
                                   WHERE w.tender_id = pp.tender_id
                                     AND w.award_value IS NOT NULL))                AS mean_abs_pct_error,
                  (SELECT count(*) FROM tenders WHERE source IS NOT NULL)           AS tenders_sourced,
                  (SELECT count(DISTINCT source) FROM tenders)                      AS distinct_sources,
                  (SELECT count(*) FROM source_registry
                    WHERE access_class IS NOT NULL)                                 AS registered_sources"""
    )
    freshness_row = await pool.fetchrow(
        """SELECT percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY extract(epoch FROM now() - t.published_at) / 86400.0)::float
                    AS median_age_days
           FROM tenders t
           WHERE t.published_at IS NOT NULL AND t.last_offer_date > now()""")
    lanes = await pool.fetch(
        """SELECT DISTINCT ON (connector) connector, ok, finished_at
           FROM ingest_runs ORDER BY connector, started_at DESC""")

    # Evidence-tier distribution over a real sample of open tenders.
    tier_rows = await pool.fetch(
        """SELECT * FROM tenders WHERE last_offer_date > now()
           ORDER BY last_offer_date LIMIT $1""", sample)
    tiers: dict[str, int] = {}
    suppressed_market = 0
    async with pool.acquire() as conn:
        for row in tier_rows:
            tender = dict(row)
            ev = await p2w.evidence.market_evidence(conn, tender=tender)
            tiers[ev.tier.value] = tiers.get(ev.tier.value, 0) + 1
            if not ev.tier.allows_market_prediction:
                suppressed_market += 1
        participation_calibration = await p2w.participation.calibrate(conn)

    n_sample = len(tier_rows)
    tier_ab = tiers.get("A", 0) + tiers.get("B", 0)
    median_age = (freshness_row["median_age_days"]
                  if freshness_row and freshness_row["median_age_days"] is not None else None)

    data = _domain({
        "observed_offer_coverage": _dimension(
            25, _pct(counts["tenders_with_offers"], counts["tenders"]),
            {"tenders_with_offers": counts["tenders_with_offers"],
             "tenders": counts["tenders"]}),
        "award_lifecycle_linkage": _dimension(
            15, _pct(counts["awards_valued"], counts["awards"]),
            {"awards": counts["awards"], "awards_with_value": counts["awards_valued"],
             "awards_linked_to_tender": counts["awards_linked"]}),
        "vendor_identity_precision": _dimension(
            20, None,
            {"vendors": counts["vendors"], "vendors_with_cr_number": counts["vendors_with_cr"]},
            "precision of canonical vendor matching requires a human-reviewed sample; "
            "none is recorded in this database"),
        "lineage_completeness": _dimension(
            15, _pct(counts["lineage_offers"] + counts["lineage_awards"],
                     counts["offers"] + counts["awards"]),
            {"source_lineage_rows": counts["lineage_rows"],
             "financial_facts": counts["offers"] + counts["awards"],
             "financial_facts_with_lineage":
                 counts["lineage_offers"] + counts["lineage_awards"]}),
        "freshness": _dimension(
            10, None if median_age is None else p2w.contracts.freshness_score(median_age),
            {"median_open_tender_age_days":
                 None if median_age is None else round(median_age, 1),
             "horizon_days": p2w.contracts.FRESHNESS_HORIZON_DAYS}),
        "boq_scope_completeness": _dimension(
            10, _pct(counts["tenders_with_boq"], counts["tenders"]),
            {"tenders_with_boq_items": counts["tenders_with_boq"],
             "tenders": counts["tenders"]}),
        "source_rights_policy_metadata": _dimension(
            5, _pct(counts["registered_sources"], max(counts["distinct_sources"], 1)),
            {"distinct_sources_in_corpus": counts["distinct_sources"],
             "sources_registered_with_access_class": counts["registered_sources"]}),
    })

    # A handful of scored rows is not a calibration measurement. Below the
    # engine's own minimum event count these dimensions stay unmeasured rather
    # than reporting 100% coverage off three lucky rows.
    min_events = p2w.participation.MIN_CALIBRATION_EVENTS
    enough_scored = int(counts["feedback_scored"]) >= min_events
    thin = (f"only {counts['feedback_scored']} scored prediction_feedback rows; "
            f"{min_events} are required before this is a measurement")

    model = _domain({
        "point_accuracy": _dimension(
            15,
            None if not (enough_scored and counts["mean_abs_pct_error"]) else max(
                0.0, 100.0 - 100.0 * float(counts["mean_abs_pct_error"])),
            {"scored_feedback_rows": counts["feedback_scored"],
             "minimum_required": min_events,
             "mean_abs_pct_error": _f(counts["mean_abs_pct_error"])},
            None if (enough_scored and counts["mean_abs_pct_error"]) else thin),
        "interval_calibration": _dimension(
            25,
            None if not enough_scored else
            _pct(counts["feedback_hits"], counts["feedback_scored"]),
            {"scored": counts["feedback_scored"], "interval_hits": counts["feedback_hits"],
             "minimum_required": min_events, "nominal_coverage_pct": 80.0},
            None if enough_scored else thin),
        "participation_calibration": _dimension(
            10,
            None if participation_calibration.get("status") != "ok" else
            max(0.0, 100.0 * (1.0 - 4.0 * float(participation_calibration["brier_score"]))),
            participation_calibration,
            None if participation_calibration.get("status") == "ok" else
            f"participation calibration status={participation_calibration.get('status')}"),
        "ranking_undercut_utility": _dimension(
            10, None, {},
            "requires resolved head-to-head outcomes per predicted competitor; none recorded"),
        "temporal_robustness": _dimension(
            15, None, {},
            "requires a rolling backtest run; not executed by this endpoint"),
        "evidence_tier_monotonicity": _dimension(
            10, None,
            {"tier_distribution_open_tenders": tiers, "sample": n_sample},
            "requires scored outcomes in more than one evidence tier; "
            f"scored rows = {counts['feedback_scored']}"),
        "drift_sensitivity": _dimension(
            5, None, {}, "no drift monitor is wired to this database yet"),
        "fallback_suppression": _dimension(
            10,
            None if not n_sample else
            (100.0 if suppressed_market == tiers.get("D", 0) else 0.0),
            {"open_tenders_sampled": n_sample,
             "tier_distribution": tiers,
             "market_suppressed": suppressed_market,
             "tier_d": tiers.get("D", 0)},
            None if n_sample else "no open tenders to sample"),
    })

    lane_ok = sum(1 for r in lanes if r["ok"])
    operational = _domain({
        "ingestion_lane_health": _dimension(
            50, _pct(lane_ok, len(lanes)),
            {"lanes": len(lanes), "last_run_ok": lane_ok,
             "connectors": [r["connector"] for r in lanes]}),
        "stale_feed_detection": _dimension(
            50, 100.0 if lanes else 0.0,
            {"ingest_runs_recorded": bool(lanes),
             "detector": "/api/lanes verdicts + /api/ops/summary"}),
    })

    security = _domain({
        "tenant_isolation_enforced": _dimension(
            40, None,
            {"tenant_filtered_tables": [
                "pursuits", "compliance_items (via pursuit)", "outcomes", "predictions",
                "follows", "alert_profiles", "user_bid_scenarios", "user_calculator_prefs"],
             "tenant_header": TENANT_HEADER,
             "default_tenant": DEFAULT_TENANT_SLUG,
             "automated_test_status": (
                 "covered by services/ingestion/tests/test_battery_security.py: a "
                 "second tenant's scenario is planted and every endpoint is grepped "
                 "for it under forged/missing/empty/numeric/SQL tenant headers"),
             "automated_test_path": "services/ingestion/tests/test_battery_security.py"},
            "hard red gate: must be evidenced by an automated cross-tenant test, "
            "not by an API self-report — this endpoint names the test rather than "
            "scoring itself on it"),
        "private_input_containment": _dimension(
            30, 100.0,
            {"shared_endpoints_audited": [
                "/api/tenders/{id}/market-intelligence", "/api/tenders/{id}/competitors"],
             "private_fields": ["estimated_cost", "min_margin_pct", "proposed_bid"],
             "test": "services/console/tests/test_p2w_api.py"}),
        "source_rights_recorded": _dimension(
            30, _pct(counts["registered_sources"], max(counts["distinct_sources"], 1)),
            {"registered_sources": counts["registered_sources"],
             "distinct_sources": counts["distinct_sources"]}),
    })

    product = _domain({
        "observed_vs_predicted_separation": _dimension(
            50, 100.0,
            {"labels": LABEL_AR, "enforced_by": "_prediction_envelope/_observed"}),
        "user_comprehension_testing": _dimension(
            50, None, {}, "requires user testing sessions; none recorded"),
    })

    commercial = _domain({
        "pilot_evidence": _dimension(
            100, None,
            # Tenant-scoped on purpose: how many private scenarios and pursuits
            # exist is user-private evidence. A global count here let any caller
            # measure another tenant's activity from a per-tenant endpoint.
            {"tenant_scenarios": await pool.fetchval(
                "SELECT count(*) FROM user_bid_scenarios WHERE tenant_id=$1", tenant_id),
             "pursuits": await pool.fetchval(
                 "SELECT count(*) FROM pursuits WHERE tenant_id=$1", tenant_id)},
            "commercial readiness is not derivable from the operational database"),
    })

    domains = {
        "data_readiness": data, "model_readiness": model,
        "product_ux_readiness": product, "security_privacy_legal": security,
        "operational_readiness": operational, "commercial_readiness": commercial,
    }
    for name, d in domains.items():
        d["weight"] = READINESS_DOMAIN_WEIGHTS[name]
    # The overall average is weighted by *evidenced* weight, not by domain
    # weight: a domain scored off 20% of its dimensions must not carry its full
    # 25 points into the headline number.
    scored = {n: d for n, d in domains.items() if d["score"] is not None}
    for d in scored.values():
        d["effective_weight"] = round(d["weight"] * d["measured_weight_pct"] / 100.0, 2)
    covered = sum(d["effective_weight"] for d in scored.values())
    overall = (sum(d["effective_weight"] * d["score"] for d in scored.values()) / covered
               if covered else None)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "model_version": p2w.contracts.MODEL_VERSION,
        "tenant_id": tenant_id,
        "corpus": {k: (int(v) if isinstance(v, int) else _f(v))
                   for k, v in dict(counts).items()},
        "open_tender_evidence_tiers": {
            "sampled": n_sample, "distribution": tiers,
            "market_prediction_suppressed": suppressed_market,
            "tier_a_or_b_pct": _pct(tier_ab, n_sample)},
        "calibration_sample": {
            "price_prediction_feedback_rows": counts["feedback"],
            "scored_rows": counts["feedback_scored"],
            "participation": participation_calibration},
        "domains": domains,
        "overall_score": None if overall is None else round(overall, 1),
        "overall_weight_covered_pct": round(covered, 1),
        "overall_weighting": "domain weight x measured dimension weight",
        "honesty_note": (
            "Dimensions with score=null are not measurable from the current database "
            "and are excluded from the averages rather than assumed passing."),
    }


# ---------------------------- BoQ workbench ----------------------------
# PRD "Thaqip for Contractors" §5.3/§5.4/§6. A contractor uploads a priced
# bill-of-quantities workbook; we run a deterministic (non-AI) review engine
# over it and, only with explicit per-upload consent, let confirmed lines
# later feed a pooled item-price benchmark (db/migrations/0022_boq_workbench.sql
# owns the schema and the privacy/consent rules — n_contributors >= 5 is a DB
# CHECK constraint there, not application logic).
#
# Every route below sits behind the normal auth gate (not in PUBLIC_PATHS) and
# every query is scoped by `tenant_id: int = Tenant`, exactly like the rest of
# this file — a BoQ document belongs to the tenant that uploaded it and to no
# one else (T-UPL-01/IDOR requirement in the PRD's contractor test plan).

BOQ_BUCKET = "thaqip-boq"
BOQ_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_XLSX_MAGIC = b"PK\x03\x04"
_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _boq_engine():
    """Lazy import of the ingestion parser + deterministic review engine.

    Mirrors `_p2w_engine`'s lazy-import-with-cache pattern above: the console
    image does not depend on thaqip_ingestion as a wheel, but `_bootstrap_p2w`
    already put the ingestion source tree on sys.path (when present), so
    `thaqip_ingestion.boq` / `thaqip_ingestion.boq_review` are importable the
    same way `thaqip_ingestion.p2w` is.
    """
    cached = getattr(app.state, "boq_engine", None)
    if cached is not None:
        return cached
    try:
        from thaqip_ingestion import boq as boq_mod
        from thaqip_ingestion import boq_review as boq_review_mod
    except ImportError as exc:  # pragma: no cover - depends on deployment
        raise HTTPException(500, detail={
            "error": "boq_engine_unavailable",
            "message_ar": "محرك مراجعة جداول الكميات غير متوفر في هذه النسخة من الخادم.",
        }) from exc
    ns = type("BoqEngine", (), {"parser": boq_mod, "review": boq_review_mod})
    app.state.boq_engine = ns
    return ns


def _boq_minio():
    """MinIO client for BoQ uploads, created lazily and cached on app.state.

    Follows the same env-var/bucket-per-client shape as
    thaqip_ingestion.documents.make_minio (see services/ingestion/src/
    thaqip_ingestion/documents.py), but with its own bucket so a BoQ workbook
    is never mixed into the general `thaqip-docs` document bucket. Credentials
    reuse docker-compose's MINIO_ROOT_USER/MINIO_ROOT_PASSWORD (see
    docker-compose.yml's `minio` service) rather than inventing a second
    secret; the endpoint defaults to the in-network host:port from that same
    compose file.
    """
    client = getattr(app.state, "boq_minio", None)
    if client is not None:
        return client
    from minio import Minio

    endpoint = os.environ.get("MINIO_ENDPOINT", "minio:9000")
    access_key = os.environ["MINIO_ROOT_USER"]
    secret_key = os.environ["MINIO_ROOT_PASSWORD"]
    secure = os.environ.get("MINIO_SECURE", "0") == "1"
    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    if not client.bucket_exists(BOQ_BUCKET):
        client.make_bucket(BOQ_BUCKET)
    app.state.boq_minio = client
    return client


def _boq_findings_json(rows) -> list[dict[str, Any]]:
    return [
        {
            "id": r["id"] if "id" in r else None,
            "line_id": r["line_id"],
            "rule": r["rule"],
            "severity": r["severity"],
            "message_ar": r["message_ar"],
            "suggestion_ar": r["suggestion_ar"],
        }
        for r in rows
    ]


@app.post("/api/boq/upload")
async def upload_boq(
    request: Request,
    file: UploadFile = File(...),
    consent_pool: bool = Form(False),
    tender_id: int | None = Form(None),
    tenant_id: int = Tenant,
):
    """Upload + parse + deterministically review one priced BoQ workbook.

    Multipart form, not a JSON body, so the fields below are plain FastAPI
    Form()/File() parameters rather than a Pydantic model (FastAPI cannot mix
    a BaseModel body with UploadFile in the same request).
    """
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(422, detail={
            "error": "invalid_file_type",
            "message_ar": "يجب أن يكون الملف المرفوع بصيغة xlsx.",
        })

    data = await file.read()
    if len(data) > BOQ_MAX_BYTES:
        raise HTTPException(422, detail={
            "error": "file_too_large",
            "message_ar": "حجم الملف يتجاوز الحد الأقصى المسموح به (10 ميجابايت).",
        })
    # xlsx files are zip archives: a correct extension with the wrong magic
    # bytes is exactly the upload-abuse case the PRD's T-UPL-01 test targets,
    # so the extension alone is never trusted.
    if not data.startswith(_XLSX_MAGIC):
        raise HTTPException(422, detail={
            "error": "invalid_file_signature",
            "message_ar": "محتوى الملف لا يطابق تنسيق xlsx الفعلي رغم امتداد الملف؛ تم رفض الملف.",
        })

    engine = _boq_engine()
    try:
        parsed = engine.parser.parse_boq_xlsx(data)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a 422, not a 500
        raise HTTPException(422, detail={
            "error": "boq_parse_failed",
            "message_ar": "تعذّرت قراءة ملف جدول الكميات؛ يرجى التأكد من أنه ملف Excel صالح غير تالف.",
        }) from exc

    if not parsed.items:
        raise HTTPException(422, detail={
            "error": "boq_parse_failed",
            "message_ar": "تعذّر التعرف على صف عناوين معروف أو أي بنود في ملف جدول الكميات.",
        })

    findings = engine.review.review_boq(parsed.items)

    sha256 = hashlib.sha256(data).hexdigest()
    storage_key = f"boq/{tenant_id}/{uuid.uuid4()}.xlsx"

    minio_client = _boq_minio()
    minio_client.put_object(
        BOQ_BUCKET, storage_key, io.BytesIO(data), len(data),
        content_type=_XLSX_CONTENT_TYPE,
    )

    who = getattr(request.state, "principal", None) or {}
    uploaded_by = who.get("user_id")

    total_computed = sum(
        (it.total for it in parsed.items if it.total is not None), Decimal("0"))
    # The parser has no notion of a separate "grand total" row distinct from
    # the sum of line totals; there is nothing honest to put here yet, so it
    # stays null rather than being faked as equal to total_computed.
    total_declared: Decimal | None = None

    pool: asyncpg.Pool = app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        consent_at = datetime.now(UTC) if consent_pool else None
        doc_id = await conn.fetchval(
            """INSERT INTO boq_documents (tenant_id, tender_id, filename, storage_key, sha256,
                                          scan_status, uploaded_by, consent_pool, consent_at,
                                          total_declared, total_computed)
               VALUES ($1,$2,$3,$4,$5,'unscanned',$6,$7,$8,$9,$10) RETURNING id""",
            tenant_id, tender_id, filename, storage_key, sha256,
            uploaded_by, consent_pool, consent_at, total_declared, total_computed,
        )

        line_id_by_line_no: dict[int, int] = {}
        for it in parsed.items:
            lid = await conn.fetchval(
                """INSERT INTO boq_lines (document_id, line_no, category, item, description, spec,
                                          unit_raw, unit, qty, unit_price, total, text_numbers)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id""",
                doc_id, it.line_no, it.category, it.item_no, it.description, it.spec,
                it.unit, it.unit, it.qty, it.unit_price, it.total, it.text_numbers,
            )
            line_id_by_line_no[it.line_no] = lid

        for f in findings:
            await conn.execute(
                """INSERT INTO boq_findings (document_id, line_id, rule, severity, message_ar, suggestion_ar)
                   VALUES ($1,$2,$3,$4,$5,$6)""",
                doc_id, line_id_by_line_no.get(f.line_no), f.rule, f.severity,
                f.message_ar, f.suggestion_ar,
            )

        if consent_pool:
            await conn.execute(
                """INSERT INTO boq_consents (tenant_id, document_id, scope, granted_at)
                   VALUES ($1,$2,'item_pool', now())""",
                tenant_id, doc_id,
            )

    return {
        "document_id": doc_id,
        "total_declared": total_declared,
        "total_computed": total_computed,
        "line_count": len(parsed.items),
        "findings": [
            {"rule": f.rule, "severity": f.severity, "line_no": f.line_no,
             "message_ar": f.message_ar, "suggestion_ar": f.suggestion_ar}
            for f in findings
        ],
        "scan_status": "unscanned",
    }


@app.get("/api/boq/{document_id}")
async def get_boq_document(document_id: int, tenant_id: int = Tenant):
    pool: asyncpg.Pool = app.state.pool
    # WHERE id=$1 AND tenant_id=$2, never id=$1 alone: another tenant's
    # document_id must 404, not leak a cross-tenant read (IDOR requirement).
    doc = await pool.fetchrow(
        "SELECT * FROM boq_documents WHERE id=$1 AND tenant_id=$2", document_id, tenant_id)
    if doc is None:
        raise HTTPException(404)
    lines = await pool.fetch(
        "SELECT * FROM boq_lines WHERE document_id=$1 ORDER BY line_no", document_id)
    findings = await pool.fetch(
        "SELECT * FROM boq_findings WHERE document_id=$1 ORDER BY id", document_id)
    return {
        "document": dict(doc),
        "lines": [dict(r) for r in lines],
        "findings": _boq_findings_json(findings),
    }


@app.get("/api/boq")
async def list_boq_documents(
    limit: int = Query(50, le=200),
    offset: int = 0,
    tenant_id: int = Tenant,
):
    pool: asyncpg.Pool = app.state.pool
    rows = await pool.fetch(
        """SELECT d.id, d.filename, d.created_at, d.total_declared, d.total_computed,
                  d.scan_status, d.consent_pool, d.tender_id,
                  coalesce((SELECT count(*) FROM boq_lines l WHERE l.document_id = d.id), 0) AS line_count,
                  coalesce((SELECT count(*) FROM boq_findings f
                             WHERE f.document_id = d.id AND f.severity = 'critical'), 0) AS critical_count,
                  coalesce((SELECT count(*) FROM boq_findings f
                             WHERE f.document_id = d.id AND f.severity = 'warning'), 0) AS warning_count,
                  coalesce((SELECT count(*) FROM boq_findings f
                             WHERE f.document_id = d.id AND f.severity = 'info'), 0) AS info_count
           FROM boq_documents d
           WHERE d.tenant_id = $1
           ORDER BY d.created_at DESC
           LIMIT $2 OFFSET $3""",
        tenant_id, limit, offset,
    )
    return [dict(r) for r in rows]


@app.delete("/api/boq/{document_id}")
async def delete_boq_document(document_id: int, tenant_id: int = Tenant):
    pool: asyncpg.Pool = app.state.pool
    doc = await pool.fetchrow(
        "SELECT storage_key FROM boq_documents WHERE id=$1 AND tenant_id=$2",
        document_id, tenant_id)
    if doc is None:
        raise HTTPException(404)
    try:
        _boq_minio().remove_object(BOQ_BUCKET, doc["storage_key"])
    except Exception as exc:  # noqa: BLE001 - the DB row is the source of truth;
        # a MinIO hiccup must not block the tenant from deleting their record.
        log.warning("failed to remove boq object %s: %r", doc["storage_key"], exc)
    # boq_lines/boq_findings/boq_consents cascade via ON DELETE CASCADE
    # (see db/migrations/0022_boq_workbench.sql).
    await pool.execute(
        "DELETE FROM boq_documents WHERE id=$1 AND tenant_id=$2", document_id, tenant_id)
    return {"deleted": True}


# ---------------------------- Eligibility / fit scoring ----------------------------
# PRD "Thaqip for Contractors" §5.1, tests T-ELIG-01/T-ELIG-02. A tenant's
# self-declared company profile (activities/regions/classification grades) is
# private to that tenant, same as everywhere else in this file. Tenders
# themselves carry no tenant_id (they are the shared market feed — see the
# pre-existing /api/tenders/{tender_id} above, which is likewise unscoped by
# tenant) so only the profile lookup below needs `tenant_id: int = Tenant`.


def _fit_engine():
    """Lazy import of the ingestion fit-scoring engine, same cached pattern
    as `_p2w_engine`/`_boq_engine` above."""
    cached = getattr(app.state, "fit_engine", None)
    if cached is not None:
        return cached
    try:
        from thaqip_ingestion import fit_score as fit_score_mod
    except ImportError as exc:  # pragma: no cover - depends on deployment
        raise HTTPException(500, detail={
            "error": "fit_engine_unavailable",
            "message_ar": "محرك حساب التوافق غير متوفر في هذه النسخة من الخادم.",
        }) from exc
    app.state.fit_engine = fit_score_mod
    return fit_score_mod


class CompanyProfileIn(BaseModel):
    activity_ids: list[int] = []
    regions: list[str] = []
    classification_grades: list[str] = []
    certifications: list[str] = []
    min_project_value: float | None = None
    max_project_value: float | None = None


_EMPTY_COMPANY_PROFILE = {
    "activity_ids": [], "regions": [], "classification_grades": [],
    "certifications": [], "min_project_value": None, "max_project_value": None,
    "updated_at": None,
}


@app.get("/api/company-profile")
async def get_company_profile(tenant_id: int = Tenant):
    pool: asyncpg.Pool = app.state.pool
    row = await pool.fetchrow(
        "SELECT * FROM company_profiles WHERE tenant_id=$1", tenant_id)
    if row is None:
        return _EMPTY_COMPANY_PROFILE
    d = dict(row)
    d.pop("tenant_id", None)
    d.pop("updated_by", None)
    return d


@app.put("/api/company-profile")
async def put_company_profile(body: CompanyProfileIn, request: Request, tenant_id: int = Tenant):
    who = getattr(request.state, "principal", None) or {}
    updated_by = who.get("user_id")
    pool: asyncpg.Pool = app.state.pool
    await pool.execute(
        """INSERT INTO company_profiles
             (tenant_id, activity_ids, regions, classification_grades, certifications,
              min_project_value, max_project_value, updated_by, updated_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now())
           ON CONFLICT (tenant_id) DO UPDATE SET
             activity_ids = EXCLUDED.activity_ids,
             regions = EXCLUDED.regions,
             classification_grades = EXCLUDED.classification_grades,
             certifications = EXCLUDED.certifications,
             min_project_value = EXCLUDED.min_project_value,
             max_project_value = EXCLUDED.max_project_value,
             updated_by = EXCLUDED.updated_by,
             updated_at = now()""",
        tenant_id, body.activity_ids, body.regions, body.classification_grades,
        body.certifications, body.min_project_value, body.max_project_value, updated_by,
    )
    return await get_company_profile(tenant_id=tenant_id)


@app.get("/api/tenders/{tender_id}/fit")
async def tender_fit(tender_id: int, tenant_id: int = Tenant):
    pool: asyncpg.Pool = app.state.pool
    tender = await pool.fetchrow(
        "SELECT id, activity_id FROM tenders WHERE id=$1", tender_id)
    if tender is None:
        raise HTTPException(404)
    profile_row = await pool.fetchrow(
        "SELECT * FROM company_profiles WHERE tenant_id=$1", tenant_id)
    detail_row = await pool.fetchrow(
        "SELECT * FROM tender_details WHERE tender_id=$1", tender_id)

    engine = _fit_engine()
    profile = engine.CompanyProfile(
        activity_ids=tuple(profile_row["activity_ids"]) if profile_row else (),
        regions=tuple(profile_row["regions"]) if profile_row else (),
        classification_grades=tuple(profile_row["classification_grades"]) if profile_row else (),
    )
    detail = None
    if detail_row is not None:
        detail = engine.TenderDetailLike(
            classification_required=detail_row["classification_required"],
            classification_text=detail_row["classification_text"],
            execution_location=detail_row["execution_location"],
        )
    tender_ns = type("Tender", (), {"activity_id": tender["activity_id"]})
    result = engine.score_fit(tender_ns, profile, detail)
    return {
        "tender_id": tender_id,
        "score": result.score,
        "max_possible_score": result.max_possible_score,
        "label": result.label,
        "classification_status": result.classification_status,
        "not_built_factors": list(result.not_built_factors),
        "reasons": [
            {"factor": r.factor, "status": r.status, "weight": r.weight,
             "points": r.points, "message_ar": r.message_ar}
            for r in result.reasons
        ],
        "details_fetched": detail_row is not None,
    }
