"""Parse Etimad's tender detail-page fragments into the structured
`tender_details` row (db/migrations/0023_eligibility.sql, PRD §5.1, T-ELIG-01).

Pure parsing lives here, separate from the actual HTTP fetch
(thaqip_ingestion.etimad.details.DetailsFetcher) and from the DB write (see
eligibility_enrich.py), so it can be tested against the real fixtures with
no network and no database.

Label lookups are substring/keyword matches against the live fixtures'
Arabic labels (services/ingestion/tests/fixtures/comp_Get*ViewComponenet.
html, confirmed 2026-09-17), not exact-string matches, because Etimad's
markup wraps labels inconsistently across tenders. `site_visit_date` has no
confirmed example in the fixtures available to this codebase; several
plausible label variants are matched, and it is left null (not fabricated)
when none appear — most tenders genuinely have no site visit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

_NOT_REQUIRED_MARKERS = ("غير مطلوب", "لا يوجد")

# label -> field name, matched by substring against the fragment's own keys.
_RELATIONS_LABELS = {
    "التصنيف": "classification_text",
    "مكان التنفيذ": "execution_location",
    "نشاط المنافسة": "activity_name_detail",
}
_DATE_LABELS = {
    "الإستفسارات": "enquiries_deadline",      # 'آخر موعد لإستلام الإستفسارات'
    "فترة التوقف": "stop_period_days",
    "المتوقع للترسية": "expected_award_date",   # 'التاريخ المتوقع للترسية'
    "بدء الأعمال": "work_start_date",           # 'تاريخ بدء الأعمال / الخدمات'
}
_SITE_VISIT_LABEL_MARKERS = ("الزيارة الميدانية", "زيارة الموقع")

_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)
_INT_RE = re.compile(r"\d+")


def _first_label_match(fields: dict[str, str], marker: str) -> str | None:
    for label, value in fields.items():
        if marker in label:
            return value
    return None


def _parse_gregorian_date(value: str) -> date | None:
    """The source shows "DD/MM/YYYY [HH:MM AM/PM] DD/MM/YYYY(Hijri)"; the
    FIRST dd/mm/yyyy occurrence is always the Gregorian date (confirmed
    against the live fixture: '18/09/2026 07/04/1448 09:59 AM' — Hijri years
    are 3 digits shorter than any Gregorian year this product will ever see,
    but matching position rather than digit count avoids relying on that).
    """
    m = _DATE_RE.search(value)
    if not m:
        return None
    day, month, year = (int(x) for x in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_gregorian_datetime(value: str) -> datetime | None:
    d = _parse_gregorian_date(value)
    if d is None:
        return None
    t = _TIME_RE.search(value)
    if not t:
        return datetime(d.year, d.month, d.day)
    hour, minute, meridiem = int(t.group(1)), int(t.group(2)), t.group(3).upper()
    if meridiem == "PM" and hour != 12:
        hour += 12
    if meridiem == "AM" and hour == 12:
        hour = 0
    return datetime(d.year, d.month, d.day, hour, minute)


def _parse_int(value: str) -> int | None:
    if any(m in value for m in _NOT_REQUIRED_MARKERS):
        return None
    m = _INT_RE.search(value)
    return int(m.group()) if m else None


@dataclass(frozen=True)
class ParsedTenderDetail:
    classification_required: bool | None = None
    classification_text: str | None = None
    execution_location: str | None = None
    activity_name_detail: str | None = None
    enquiries_deadline: datetime | None = None
    stop_period_days: int | None = None
    expected_award_date: date | None = None
    work_start_date: date | None = None
    site_visit_date: datetime | None = None
    relations_raw: dict[str, str] = field(default_factory=dict)
    dates_raw: dict[str, str] = field(default_factory=dict)


def parse_detail_fields(relations: dict[str, str], dates: dict[str, str]) -> ParsedTenderDetail:
    classification_text = _first_label_match(relations, "التصنيف")
    classification_required = (
        not any(m in classification_text for m in _NOT_REQUIRED_MARKERS)
        if classification_text is not None else None
    )
    execution_location = _first_label_match(relations, "مكان التنفيذ")
    activity_name_detail = _first_label_match(relations, "نشاط المنافسة")

    enquiries_raw = _first_label_match(dates, "الإستفسارات")
    stop_period_raw = _first_label_match(dates, "فترة التوقف")
    expected_award_raw = _first_label_match(dates, "المتوقع للترسية")
    work_start_raw = _first_label_match(dates, "بدء الأعمال")
    site_visit_raw = None
    for marker in _SITE_VISIT_LABEL_MARKERS:
        site_visit_raw = _first_label_match(dates, marker)
        if site_visit_raw is not None:
            break

    return ParsedTenderDetail(
        classification_required=classification_required,
        classification_text=classification_text,
        execution_location=execution_location,
        activity_name_detail=activity_name_detail,
        enquiries_deadline=_parse_gregorian_datetime(enquiries_raw) if enquiries_raw else None,
        stop_period_days=_parse_int(stop_period_raw) if stop_period_raw else None,
        expected_award_date=_parse_gregorian_date(expected_award_raw) if expected_award_raw else None,
        work_start_date=_parse_gregorian_date(work_start_raw) if work_start_raw else None,
        site_visit_date=_parse_gregorian_datetime(site_visit_raw) if site_visit_raw else None,
        relations_raw=dict(relations),
        dates_raw=dict(dates),
    )
