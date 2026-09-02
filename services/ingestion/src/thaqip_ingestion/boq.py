"""BOQ parser v1 (ticket C7): XLSX bill-of-quantities -> structured line items.

Etimad BOQ workbooks vary by agency, but converge on Arabic header rows
containing some of: رقم البند / م، وصف البند / البيان / الوصف، الوحدة،
الكمية، (سعر الوحدة، الإجمالي — priced columns exist in offer forms and are
ignored here). Strategy:

1. Scan the first 15 rows of each sheet for the header row (>= 2 recognized
   Arabic column labels).
2. Map columns by label; read data rows until a long empty streak.
3. Per-file confidence = recognized-columns coverage x parsed-row ratio;
   rows lacking a description are dropped.

Files below MIN_CONFIDENCE go to the review queue (documents row keeps
text for manual mapping) instead of polluting boq_items.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

log = logging.getLogger("thaqip.boq")

MIN_CONFIDENCE = 0.5
_MAX_HEADER_SCAN = 15
_EMPTY_STREAK_STOP = 20

_COLUMN_PATTERNS: dict[str, list[str]] = {
    "item_no": [r"^م$", r"رقم\s*البند", r"^البند$", r"^رقم$", r"^#$", r"item"],
    "description": [r"وصف", r"البيان", r"بيان\s*الأعمال", r"الأعمال", r"description"],
    "unit": [r"الوحدة", r"وحدة\s*القياس", r"unit"],
    "qty": [r"الكمية", r"كمية", r"qty", r"quantity"],
}


def _norm(cell) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


def _match_column(label: str) -> str | None:
    for field, patterns in _COLUMN_PATTERNS.items():
        for p in patterns:
            if re.search(p, label, re.I):
                return field
    return None


def _to_decimal(cell) -> Decimal | None:
    if cell is None:
        return None
    if isinstance(cell, (int, float, Decimal)):
        try:
            return Decimal(str(cell))
        except InvalidOperation:
            return None
    s = _norm(cell).replace(",", "").replace("٫", ".")
    # Arabic-Indic digits
    s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


@dataclass
class BoqItem:
    item_no: str | None
    description: str
    unit: str | None
    qty: Decimal | None


@dataclass
class BoqParseResult:
    items: list[BoqItem]
    confidence: float
    sheets_parsed: int
    notes: list[str]


def parse_boq_xlsx(data: bytes) -> BoqParseResult:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    items: list[BoqItem] = []
    notes: list[str] = []
    sheets_parsed = 0
    best_coverage = 0.0
    total_rows = 0
    kept_rows = 0

    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header_map: dict[int, str] | None = None
        for row_idx, row in enumerate(rows):
            if row_idx >= _MAX_HEADER_SCAN:
                break
            candidate: dict[int, str] = {}
            for col_idx, cell in enumerate(row or ()):
                field = _match_column(_norm(cell))
                if field and field not in candidate.values():
                    candidate[col_idx] = field
            if len(candidate) >= 2 and "description" in candidate.values():
                header_map = candidate
                break
        if header_map is None:
            notes.append(f"sheet '{ws.title}': no header row recognized")
            continue

        sheets_parsed += 1
        coverage = len(header_map) / len(_COLUMN_PATTERNS)
        best_coverage = max(best_coverage, coverage)
        inv = {v: k for k, v in header_map.items()}
        empty_streak = 0
        for row in rows:  # continues after the header row
            total_rows += 1
            get = lambda f: (row[inv[f]] if f in inv and inv[f] < len(row) else None)  # noqa: E731
            desc = _norm(get("description"))
            if not desc:
                empty_streak += 1
                if empty_streak >= _EMPTY_STREAK_STOP:
                    break
                continue
            empty_streak = 0
            if _match_column(desc):  # repeated header row inside the sheet
                continue
            items.append(BoqItem(
                item_no=_norm(get("item_no")) or None,
                description=desc,
                unit=_norm(get("unit")) or None,
                qty=_to_decimal(get("qty")),
            ))
            kept_rows += 1

    row_ratio = (kept_rows / total_rows) if total_rows else 0.0
    confidence = round(best_coverage * (0.5 + 0.5 * row_ratio), 3) if items else 0.0
    return BoqParseResult(items=items, confidence=confidence,
                          sheets_parsed=sheets_parsed, notes=notes)


async def store_boq(pool, *, tender_pk: int, document_id: int | None,
                    result: BoqParseResult) -> int:
    """Replace boq_items for a tender from a parse result. Returns rows stored."""
    if result.confidence < MIN_CONFIDENCE:
        log.warning("BOQ confidence %.2f below threshold; sending to review queue",
                    result.confidence)
        return 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM boq_items WHERE tender_id = $1 AND document_id IS NOT DISTINCT FROM $2",
                tender_pk, document_id,
            )
            for it in result.items:
                await conn.execute(
                    """INSERT INTO boq_items (tender_id, document_id, item_no, description,
                                              unit, qty, confidence)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    tender_pk, document_id, it.item_no, it.description,
                    it.unit, it.qty, result.confidence,
                )
    return len(result.items)
