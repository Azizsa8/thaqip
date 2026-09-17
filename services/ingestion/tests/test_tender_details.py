"""Tests for thaqip_ingestion.tender_details parsing against the real
Etimad detail-page fixtures (services/ingestion/tests/fixtures), the same
fixtures test_details.py already exercises for the raw fragment parser.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from thaqip_ingestion.etimad.details import parse_fragment
from thaqip_ingestion.tender_details import parse_detail_fields

FIX = Path(__file__).parent / "fixtures"


def _load():
    relations = parse_fragment((FIX / "comp_GetRelationsDetailsViewComponenet.html").read_text())
    dates = parse_fragment((FIX / "comp_GetTenderDatesViewComponenet.html").read_text())
    return parse_detail_fields(relations, dates)


def test_classification_not_required_on_fixture_tender():
    detail = _load()
    assert detail.classification_required is False
    assert detail.classification_text == "غير مطلوب"


def test_execution_location_parsed():
    detail = _load()
    assert detail.execution_location == "داخل المملكة منطقة الرياض الرياض"


def test_activity_name_detail_parsed():
    detail = _load()
    assert detail.activity_name_detail == "تقنية المعلومات"


def test_enquiries_deadline_parsed_as_gregorian():
    detail = _load()
    assert detail.enquiries_deadline == datetime(2026, 9, 12)


def test_stop_period_days_parsed():
    detail = _load()
    assert detail.stop_period_days == 5


def test_expected_award_date_parsed():
    detail = _load()
    assert detail.expected_award_date == date(2026, 10, 11)


def test_work_start_date_parsed():
    detail = _load()
    assert detail.work_start_date == date(2026, 11, 11)


def test_site_visit_date_absent_on_fixture_tender_is_none_not_fabricated():
    detail = _load()
    assert detail.site_visit_date is None


def test_raw_dicts_retained_losslessly():
    detail = _load()
    assert "مجال التصنيف" in detail.relations_raw
    assert "آخر موعد لتقديم العروض" in detail.dates_raw


def test_time_of_day_parsed_for_deadline_with_am_pm():
    # 'آخر موعد لتقديم العروض' carries a time; enquiries deadline in this
    # fixture does not, so verify AM/PM handling directly via the dates dict.
    relations = parse_fragment((FIX / "comp_GetRelationsDetailsViewComponenet.html").read_text())
    dates = parse_fragment((FIX / "comp_GetTenderDatesViewComponenet.html").read_text())
    raw = next(v for k, v in dates.items() if "لتقديم العروض" in k)
    from thaqip_ingestion.tender_details import _parse_gregorian_datetime
    parsed = _parse_gregorian_datetime(raw)
    assert parsed == datetime(2026, 9, 18, 9, 59)
