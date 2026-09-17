"""Deterministic (non-AI) BoQ review rules engine (ticket C7 / PRD 5.3).

Takes the parsed line items produced by ``thaqip_ingestion.boq.parse_boq_xlsx``
(or any duck-typed object exposing the same attributes: ``line_no``,
``description``, ``spec``, ``category``, ``unit``, ``qty``, ``unit_price``,
``total``, ``text_numbers``) and returns a list of :class:`Finding` objects.

Pure and side-effect free by design: no DB access, no I/O, no AI calls, so it
is independently unit-testable and reproducible.
"""
from __future__ import annotations

import difflib
import itertools
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

Severity = str  # 'info' | 'warning' | 'critical'


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Severity
    line_no: int | None
    message_ar: str
    suggestion_ar: str | None = None


_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# Capacity-like tokens: number followed by a unit token. Includes the Latin
# abbreviations named in the spec plus the Arabic "ك.ف.أ" (kilo-volt-ampere /
# kVA) abbreviation actually used in the real-world reference BoQ.
_CAPACITY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kva|kw|hp|أمبير|amp|mm|مم|ك\.?\s*ف\.?\s*أ)",
    re.IGNORECASE,
)

# Gulf-style cable cross-section specs, e.g. "قطاع (4× 50 مم2+25 أرضي)" or
# "قطاع (4 × 70 + 35مم2 أرضي)": a conductor-count multiplier, the MAIN
# conductor's cross-section, and an optional "+ K أرضي" ground-conductor
# clause, all inside one parenthetical.
_CABLE_QITA_RE = re.compile(r"قطاع\s*\(([^)]*)\)")
# Whichever number sits immediately before "أرضي" (with an optional مم/مم2
# unit token in between) is the GROUND conductor's size, never the item's
# rated/main cross-section.
_CABLE_GROUND_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:مم\d*)?\s*أرضي")

_VAT_KEYWORDS = ("ضريبة", "قيمة مضافة", "vat")

_ARITHMETIC_TOLERANCE = Decimal("1")
_ROUNDING_TOLERANCE = 1e-9


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _norm_key(description: str) -> str:
    return _text(description).lower()[:60]


def review_boq(lines: Sequence[Any]) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_check_arithmetic_and_prices(lines))
    findings.extend(_check_text_numbers(lines))
    findings.extend(_check_duplicate_descriptions(lines))
    findings.extend(_check_missing_spec(lines))
    findings.extend(_check_capacity_inversion(lines))
    findings.extend(_check_concentration_risk(lines))
    findings.extend(_check_vat_missing(lines))
    return findings


def _check_arithmetic_and_prices(lines: Iterable[Any]) -> list[Finding]:
    out: list[Finding] = []
    for ln in lines:
        qty = _num(getattr(ln, "qty", None))
        unit_price = _num(getattr(ln, "unit_price", None))
        total = _num(getattr(ln, "total", None))
        line_no = getattr(ln, "line_no", None)

        if qty is not None and unit_price is not None and total is not None:
            expected = qty * unit_price
            if abs(expected - total) > float(_ARITHMETIC_TOLERANCE):
                out.append(Finding(
                    rule="arithmetic_mismatch",
                    severity="critical",
                    line_no=line_no,
                    message_ar=(
                        f"البند رقم {line_no}: الإجمالي المذكور ({total:,.2f}) لا يطابق "
                        f"حاصل ضرب الكمية في سعر الوحدة ({expected:,.2f})."
                    ),
                    suggestion_ar="يرجى مراجعة الكمية أو سعر الوحدة أو الإجمالي وتصحيح القيمة الخاطئة.",
                ))

        if unit_price is not None and abs(unit_price - round(unit_price, 2)) > _ROUNDING_TOLERANCE:
            out.append(Finding(
                rule="unrounded_unit_price",
                severity="info",
                line_no=line_no,
                message_ar=(
                    f"البند رقم {line_no}: سعر الوحدة ({unit_price}) يحتوي على أكثر من "
                    "خانتين عشريتين، مما يوحي بأنه محسوب عكسيًا من مبلغ إجمالي (سعر مقطوعية)."
                ),
                suggestion_ar="يرجى التأكد من أن سعر الوحدة قيمة حقيقية وليست ناتج قسمة تقريبية.",
            ))
    return out


def _check_text_numbers(lines: Iterable[Any]) -> list[Finding]:
    out: list[Finding] = []
    for ln in lines:
        if getattr(ln, "text_numbers", False):
            line_no = getattr(ln, "line_no", None)
            out.append(Finding(
                rule="text_number",
                severity="info",
                line_no=line_no,
                message_ar=(
                    f"البند رقم {line_no}: الكمية و/أو سعر الوحدة مخزّنة كنص في ملف الإكسل "
                    "وليست كأرقام."
                ),
                suggestion_ar="يفضل تحويل هذه الخلايا إلى تنسيق رقمي في ملف المصدر لتفادي أخطاء الحساب.",
            ))
    return out


def _check_duplicate_descriptions(lines: Sequence[Any]) -> list[Finding]:
    out: list[Finding] = []
    groups: dict[str, list[Any]] = {}
    for ln in lines:
        desc = _text(getattr(ln, "description", None))
        if not desc:
            continue
        groups.setdefault(_norm_key(desc), []).append(ln)

    for group in groups.values():
        if len(group) < 2:
            continue
        for a, b in itertools.combinations(group, 2):
            pa = _num(getattr(a, "unit_price", None))
            pb = _num(getattr(b, "unit_price", None))
            if pa is None or pb is None:
                continue
            if abs(pa - pb) < 1e-9:
                continue
            la, lb = getattr(a, "line_no", None), getattr(b, "line_no", None)
            anchor = min(x for x in (la, lb) if x is not None) if (la is not None or lb is not None) else None
            out.append(Finding(
                rule="duplicate_description",
                severity="warning",
                line_no=anchor,
                message_ar=(
                    f"البند رقم {la} والبند رقم {lb} لهما نفس الوصف تقريبًا لكن بسعر وحدة "
                    f"مختلف ({pa:,.2f} مقابل {pb:,.2f})."
                ),
                suggestion_ar="يرجى التأكد من أن اختلاف السعر مبرر (مواصفات مختلفة) أو توحيد السعر.",
            ))
    return out


def _check_missing_spec(lines: Iterable[Any]) -> list[Finding]:
    out: list[Finding] = []
    for ln in lines:
        spec = _text(getattr(ln, "spec", None))
        if not spec:
            line_no = getattr(ln, "line_no", None)
            out.append(Finding(
                rule="missing_spec",
                severity="warning",
                line_no=line_no,
                message_ar=f"البند رقم {line_no}: لا يوجد وصف مواصفات لهذا البند.",
                suggestion_ar="يرجى إضافة مواصفات فنية واضحة لتفادي الغموض عند التنفيذ أو التسعير.",
            ))
    return out


def _normalize_for_similarity(text: str) -> str:
    t = _text(text).translate(_ARABIC_INDIC_DIGITS).lower()
    t = _CAPACITY_RE.sub(" ", t)
    return t


def _boilerplate_words(descriptions: Iterable[str]) -> set[str]:
    """Words that show up in most lines' descriptions (e.g. every BoQ line
    starting with "توريد وتركيب") are boilerplate, not evidence that two
    lines describe the *same kind* of equipment. Excluding them keeps the
    capacity_inversion pairing heuristic from matching almost everything.
    """
    descs = [d for d in descriptions if d]
    n = len(descs)
    if n == 0:
        return set()
    doc_freq: dict[str, int] = {}
    for d in descs:
        for w in {w for w in _normalize_for_similarity(d).split() if len(w) >= 4}:
            doc_freq[w] = doc_freq.get(w, 0) + 1
    threshold = max(2, int(n * 0.15))
    return {w for w, freq in doc_freq.items() if freq >= threshold}


def _similar(a: str, b: str, stop: frozenset[str] = frozenset()) -> bool:
    na, nb = _normalize_for_similarity(a), _normalize_for_similarity(b)
    if not na or not nb:
        return False

    words_a = {w for w in na.split() if len(w) >= 4 and w not in stop}
    words_b = {w for w in nb.split() if len(w) >= 4 and w not in stop}
    if len(words_a & words_b) >= 2:
        return True

    # Fall back to whole-string similarity, but on boilerplate-stripped text
    # so a shared generic opening phrase can't inflate the ratio on its own.
    stripped_a = " ".join(w for w in na.split() if w not in stop)
    stripped_b = " ".join(w for w in nb.split() if w not in stop)
    if not stripped_a or not stripped_b:
        return False
    ratio = difflib.SequenceMatcher(None, stripped_a, stripped_b).ratio()
    return ratio > 0.7


def _cable_section_capacity(text: str) -> float | None:
    """Main conductor cross-section (mm²) from a "قطاع (...)" cable spec.

    The source prose attaches the "مم2" unit label to whichever number
    happens to sit next to it in free text — sometimes the main conductor's
    size, sometimes the ground conductor's — so a plain "number immediately
    before مم" regex can silently grab the wrong one whenever a "+ K أرضي"
    clause separates the main number from its own unit token. (Observed in
    the real reference BoQ: "قطاع (4 × 70 + 35مم2 أرضي)" is a 70mm² cable
    with a 35mm² ground wire, not a 35mm² cable — the naive regex read "35".)

    This explicitly identifies the ground number by its "أرضي" neighbour and
    returns the largest of what remains, since every other number inside the
    parenthetical is either the main size or a conductor-count multiplier
    (always smaller than the size it multiplies).
    """
    m = _CABLE_QITA_RE.search(text)
    if not m:
        return None
    inner = m.group(1)
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", inner)]
    if not nums:
        return None
    ground = _CABLE_GROUND_RE.search(inner)
    ground_val = float(ground.group(1)) if ground else None
    candidates = [n for n in nums if n != ground_val]
    return max(candidates) if candidates else None


def _extract_capacity(text: str) -> tuple[float, str] | None:
    t = _text(text).translate(_ARABIC_INDIC_DIGITS)
    cable = _cable_section_capacity(t)
    if cable is not None:
        return cable, "مم"
    m = _CAPACITY_RE.search(t)
    if not m:
        return None
    return float(m.group(1)), m.group(2).lower().replace(" ", "")


def _check_capacity_inversion(lines: Sequence[Any]) -> list[Finding]:
    out: list[Finding] = []
    candidates: list[tuple[Any, float, str, str]] = []
    for ln in lines:
        desc = _text(getattr(ln, "description", None))
        spec = _text(getattr(ln, "spec", None))
        combined = f"{desc} {spec}".strip()
        cap = _extract_capacity(combined)
        if cap is not None:
            candidates.append((ln, cap[0], cap[1], desc))

    stop = frozenset(_boilerplate_words(getattr(ln, "description", None) for ln in lines))

    seen_pairs: set[tuple[int, int]] = set()
    for (line_a, cap_a, unit_a, desc_a), (line_b, cap_b, unit_b, desc_b) in itertools.combinations(candidates, 2):
        if unit_a != unit_b:
            continue
        if cap_a == cap_b:
            continue
        if not _similar(desc_a, desc_b, stop):
            continue

        price_a = _num(getattr(line_a, "unit_price", None))
        price_b = _num(getattr(line_b, "unit_price", None))
        if price_a is None or price_b is None:
            continue

        if cap_a > cap_b:
            bigger, smaller = (line_a, cap_a, price_a), (line_b, cap_b, price_b)
        else:
            bigger, smaller = (line_b, cap_b, price_b), (line_a, cap_a, price_a)

        if bigger[2] < smaller[2]:
            key = tuple(sorted((getattr(line_a, "line_no", None) or 0, getattr(line_b, "line_no", None) or 0)))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            out.append(Finding(
                rule="capacity_inversion",
                severity="critical",
                line_no=getattr(bigger[0], "line_no", None),
                message_ar=(
                    f"عكس في التسعير حسب السعة بين البند رقم {getattr(bigger[0], 'line_no', None)} "
                    f"(سعة {bigger[1]:g} {unit_a}, سعر {bigger[2]:,.2f}) والبند رقم "
                    f"{getattr(smaller[0], 'line_no', None)} (سعة {smaller[1]:g} {unit_a}, سعر "
                    f"{smaller[2]:,.2f}): الوحدة الأكبر سعة أرخص من الوحدة الأصغر."
                ),
                suggestion_ar="يرجى من المُقدِّر التأكد من مطابقة كل سعر للسعة الصحيحة قبل الاعتماد.",
            ))
    return out


def _check_concentration_risk(lines: Sequence[Any]) -> list[Finding]:
    totals = [(_num(getattr(ln, "total", None))) for ln in lines]
    totals = [t for t in totals if t is not None]
    if not totals:
        return []
    grand = sum(totals)
    if grand <= 0:
        return []
    top10 = sorted(totals, reverse=True)[:10]
    pct = sum(top10) / grand * 100
    if pct > 50:
        return [Finding(
            rule="concentration_risk",
            severity="warning",
            line_no=None,
            message_ar=(
                f"أعلى 10 بنود من حيث القيمة تمثل {pct:.1f}% من إجمالي قيمة جدول الكميات، "
                "مما يعني أن مخاطر التسعير مركزة في عدد قليل من البنود."
            ),
            suggestion_ar="يوصى بمراجعة تسعير أكبر 10 بنود بعناية إضافية نظرًا لتأثيرها الكبير على الإجمالي.",
        )]
    return []


def _check_vat_missing(lines: Iterable[Any]) -> list[Finding]:
    for ln in lines:
        blob = " ".join(
            _text(getattr(ln, f, None))
            for f in ("description", "spec", "category")
        ).lower()
        for kw in _VAT_KEYWORDS:
            if kw.lower() in blob:
                return []
    return [Finding(
        rule="vat_missing",
        severity="info",
        line_no=None,
        message_ar=(
            "لم يُعثر على أي إشارة إلى ضريبة القيمة المضافة في بنود جدول الكميات؛ "
            "يرجى التأكيد مع العميل حول كيفية معاملة الضريبة (مستثناة أو مضمنة في الأسعار)."
        ),
        suggestion_ar=None,
    )]
