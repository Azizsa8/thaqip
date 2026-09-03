"""War Room v0 (PRD M3-1/M3-2): pursuit hydration + compliance matrix.

Two extraction origins, honestly labeled on every row:

* **rule** — deterministic baseline available for every tender today: the
  document set mandated by the Government Tenders & Procurement Law (GTPL)
  and its regulations, plus items derived from the tender's own structured
  fields (deadlines, classification requirement, booklet purchase, bond
  visibility). `source_ref` cites the regulation article or the tender field.
* **llm** — clause-level extraction from TSD text via the model gateway
  (`llm.py`); activates when THAQIP_ANTHROPIC_API_KEY is configured AND the
  tender has extracted document text. Rows carry model confidence and a
  file/clause citation. (M3-2 target: >=95% precision, human-editable.)

Everything is editable in the console; manual edits get origin='manual'.
"""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

log = logging.getLogger("thaqip.warroom")

# GTPL-mandated bid documents (نظام المنافسات والمشتريات الحكومية ولوائحه).
GTPL_BASELINE: list[dict[str, Any]] = [
    dict(requirement="سجل تجاري ساري المفعول", category="document",
         source_ref="نظام المنافسات والمشتريات الحكومية — متطلبات التأهيل", sort_order=10),
    dict(requirement="شهادة تسديد الزكاة والدخل سارية", category="document",
         source_ref="نظام المنافسات — المادة (شروط تقديم العطاء)", sort_order=11),
    dict(requirement="شهادة اشتراك الغرفة التجارية سارية", category="document",
         source_ref="نظام المنافسات — شروط تقديم العطاء", sort_order=12),
    dict(requirement="شهادة التأمينات الاجتماعية (GOSI)", category="document",
         source_ref="متطلبات التأهيل النظامية", sort_order=13),
    dict(requirement="شهادة السعودة / نطاقات", category="document",
         source_ref="متطلبات التأهيل النظامية", sort_order=14),
    dict(requirement="خطاب تقديم موقّع بالإقرار بالاطلاع على كراسة الشروط",
         category="document", source_ref="نظام المنافسات — شروط تقديم العطاء", sort_order=15),
    dict(requirement="بيان الأعمال السابقة المماثلة", category="qualification",
         source_ref="نظام المنافسات — تقييم القدرات", sort_order=20),
]


def field_requirements(tender: dict) -> list[dict[str, Any]]:
    """Requirements derived from the tender's own structured fields."""
    items: list[dict[str, Any]] = []
    if tender.get("last_enquiries_date"):
        items.append(dict(
            requirement=f"إرسال الاستفسارات قبل {str(tender['last_enquiries_date'])[:10]}",
            category="deadline", source_ref="بيانات المنافسة — آخر موعد للاستفسارات",
            sort_order=1))
    if tender.get("last_offer_date"):
        items.append(dict(
            requirement=f"تقديم العرض قبل {str(tender['last_offer_date'])[:16]}",
            category="deadline", source_ref="بيانات المنافسة — آخر موعد لتقديم العروض",
            sort_order=2))
    booklet = tender.get("booklet_price")
    if booklet and float(booklet) > 0:
        items.append(dict(
            requirement=f"شراء كراسة الشروط ({booklet:,.0f} ر.س) عبر اعتماد",
            category="document", source_ref="بيانات المنافسة — قيمة الكراسة", sort_order=5))
    if tender.get("source") == "etimad" and (tender.get("tender_type_id") == 1):
        items.append(dict(
            requirement="ضمان ابتدائي بنسبة 1%–2% من قيمة العطاء (ما لم تُعفِ الكراسة)",
            category="guarantee", source_ref="نظام المنافسات — الضمان الابتدائي (منافسة عامة)",
            sort_order=30))
    activity = (tender.get("activity_name_raw") or "")
    if any(k in activity for k in ("إنشاء", "مقاول", "تشييد", "بناء")):
        items.append(dict(
            requirement="شهادة تصنيف المقاولين في المجال والدرجة المطلوبة",
            category="qualification", source_ref="اشتراط التصنيف لأنشطة الإنشاءات",
            sort_order=21))
    return items


async def create_pursuit(pool: asyncpg.Pool, tender_id: int) -> dict:
    """Create (or return existing) pursuit and hydrate the baseline matrix."""
    tender = await pool.fetchrow("SELECT * FROM tenders WHERE id=$1", tender_id)
    if tender is None:
        raise ValueError("tender not found")
    tender = dict(tender)

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval(
                "SELECT id FROM pursuits WHERE tender_id=$1", tender_id
            )
            if existing:
                return {"id": existing, "created": False}
            pid = await conn.fetchval(
                "INSERT INTO pursuits (tender_id) VALUES ($1) RETURNING id", tender_id
            )
            for item in field_requirements(tender) + GTPL_BASELINE:
                await conn.execute(
                    """INSERT INTO compliance_items
                         (pursuit_id, requirement, category, source_ref, origin, sort_order)
                       VALUES ($1,$2,$3,$4,'rule',$5)""",
                    pid, item["requirement"], item["category"],
                    item["source_ref"], item["sort_order"],
                )
            await conn.execute(
                """INSERT INTO ingest_events (event_type, entity_type, entity_id, data)
                   VALUES ('pursuit.created', 'pursuit', $1, '{}')""", pid,
            )
    log.info("pursuit %s created for tender %s with baseline matrix", pid, tender_id)
    return {"id": pid, "created": True}


async def llm_extract(pool: asyncpg.Pool, pursuit_id: int) -> dict:
    """Run clause-level LLM extraction over the pursuit's document text (M3-2).

    No-ops with a clear reason when the gateway is unconfigured or the tender
    has no extracted text yet (documents are login-gated on Etimad — F2/M1-7).
    """
    from .llm import LLMGateway

    gw = LLMGateway.from_env()
    if gw is None:
        return {"status": "skipped", "reason": "llm gateway not configured (THAQIP_ANTHROPIC_API_KEY)"}
    tender_id = await pool.fetchval("SELECT tender_id FROM pursuits WHERE id=$1", pursuit_id)
    chunks = await pool.fetch(
        """SELECT c.content FROM doc_chunks c
           JOIN documents d ON d.id = c.document_id
           WHERE d.tender_id = $1 ORDER BY c.document_id, c.chunk_no LIMIT 40""",
        tender_id,
    )
    if not chunks:
        return {"status": "skipped", "reason": "no extracted document text for this tender"}
    items = await gw.extract_compliance("\n\n".join(c["content"] for c in chunks))
    stored = 0
    for it in items:
        await pool.execute(
            """INSERT INTO compliance_items
                 (pursuit_id, requirement, category, source_ref, origin, confidence, sort_order)
               VALUES ($1,$2,$3,$4,'llm',$5,50)""",
            pursuit_id, it["requirement"], it.get("category", "technical"),
            it.get("source_ref", "TSD"), float(it.get("confidence", 0.7)),
        )
        stored += 1
    return {"status": "ok", "extracted": stored}
