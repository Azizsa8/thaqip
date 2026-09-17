"""Tests for the deterministic BoQ review rules engine (thaqip_ingestion.boq_review).

Synthetic-fixture tests (built in-code, no external files) cover the happy
path and the trigger condition for each rule and must pass unconditionally on
any machine.

The final test class, marked clearly below, additionally exercises the real
hospital-rehab reference BoQ (93 priced lines, third-party private data kept
outside the repo). It reads the file via THAQIP_BOQ_FIXTURE / the well-known
absolute path and skips itself when that file is not present, mirroring the
skip pattern used by other live-data tests in this suite (see
tests/test_battery_security.py and friends for `pytest.skip(...)`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

import pytest

from thaqip_ingestion.boq import parse_boq_xlsx
from thaqip_ingestion.boq_review import review_boq


@dataclass
class Line:
    """Minimal duck-typed BoQ line record for synthetic fixtures."""
    line_no: int
    description: str
    spec: str | None = None
    category: str | None = None
    unit: str | None = "عدد"
    qty: float | None = 1
    unit_price: float | None = 0
    total: float | None = 0
    text_numbers: bool = False


def _findings_of(findings, rule):
    return [f for f in findings if f.rule == rule]


# ---------------------------------------------------------------------------
# arithmetic_mismatch
# ---------------------------------------------------------------------------

def test_arithmetic_ok_no_mismatch_finding():
    lines = [Line(1, "بند سليم", spec="مواصفات", qty=10, unit_price=5, total=50)]
    findings = review_boq(lines)
    assert _findings_of(findings, "arithmetic_mismatch") == []


def test_arithmetic_mismatch_flagged():
    lines = [Line(1, "بند به خطأ", spec="مواصفات", qty=10, unit_price=5, total=999)]
    findings = _findings_of(review_boq(lines), "arithmetic_mismatch")
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].line_no == 1


def test_arithmetic_mismatch_within_tolerance_not_flagged():
    # 10 * 5 = 50; stated total 50.7 is within the 1 SAR rounding tolerance
    lines = [Line(1, "بند", spec="s", qty=10, unit_price=5, total=50.7)]
    assert _findings_of(review_boq(lines), "arithmetic_mismatch") == []


# ---------------------------------------------------------------------------
# unrounded_unit_price
# ---------------------------------------------------------------------------

def test_unrounded_unit_price_clean_price_not_flagged():
    lines = [Line(1, "بند", spec="s", qty=1, unit_price=100.50, total=100.50)]
    assert _findings_of(review_boq(lines), "unrounded_unit_price") == []


def test_unrounded_unit_price_backsolved_flagged():
    lines = [Line(1, "بند", spec="s", qty=1, unit_price=11097.391304347828, total=11097.39)]
    findings = _findings_of(review_boq(lines), "unrounded_unit_price")
    assert len(findings) == 1
    assert findings[0].severity == "info"
    assert findings[0].line_no == 1


# ---------------------------------------------------------------------------
# text_number
# ---------------------------------------------------------------------------

def test_text_number_not_flagged_when_numeric():
    lines = [Line(1, "بند", spec="s", qty=10, unit_price=5, total=50, text_numbers=False)]
    assert _findings_of(review_boq(lines), "text_number") == []


def test_text_number_flagged():
    lines = [Line(1, "بند", spec="s", qty=10, unit_price=5, total=50, text_numbers=True)]
    findings = _findings_of(review_boq(lines), "text_number")
    assert len(findings) == 1
    assert findings[0].severity == "info"
    assert findings[0].line_no == 1


# ---------------------------------------------------------------------------
# duplicate_description
# ---------------------------------------------------------------------------

def test_duplicate_description_same_price_not_flagged():
    lines = [
        Line(1, "توريد وتركيب باب حديد مقاوم للحريق", spec="s", unit_price=100),
        Line(2, "توريد وتركيب باب حديد مقاوم للحريق", spec="s", unit_price=100),
    ]
    assert _findings_of(review_boq(lines), "duplicate_description") == []


def test_duplicate_description_different_price_flagged():
    lines = [
        Line(1, "توريد وتركيب باب حديد مقاوم للحريق", spec="s", unit_price=100),
        Line(2, "توريد وتركيب باب حديد مقاوم للحريق", spec="s", unit_price=250),
    ]
    findings = _findings_of(review_boq(lines), "duplicate_description")
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "warning"
    assert "1" in f.message_ar and "2" in f.message_ar
    assert "100" in f.message_ar and "250" in f.message_ar


# ---------------------------------------------------------------------------
# missing_spec
# ---------------------------------------------------------------------------

def test_missing_spec_present_not_flagged():
    lines = [Line(1, "بند", spec="مواصفات مفصلة")]
    assert _findings_of(review_boq(lines), "missing_spec") == []


def test_missing_spec_blank_flagged():
    lines = [Line(1, "بند", spec=""), Line(2, "بند آخر", spec="   ")]
    findings = _findings_of(review_boq(lines), "missing_spec")
    assert {f.line_no for f in findings} == {1, 2}
    assert all(f.severity == "warning" for f in findings)


# ---------------------------------------------------------------------------
# capacity_inversion
# ---------------------------------------------------------------------------

def test_capacity_inversion_normal_pricing_not_flagged():
    lines = [
        Line(1, "وحدة تكييف", spec="وحدة تكييف سعة 30 kva", unit_price=1000),
        Line(2, "وحدة تكييف", spec="وحدة تكييف سعة 60 kva", unit_price=2000),
    ]
    assert _findings_of(review_boq(lines), "capacity_inversion") == []


def test_capacity_inversion_flagged_when_bigger_is_cheaper():
    lines = [
        Line(55, "وحدة عدم انقطاع التيار الكهربي", spec="UPS قدرة 60 kva", unit_price=71300),
        Line(56, "وحدة عدم انقطاع التيار الكهربي", spec="UPS قدرة 30 kva", unit_price=97240),
    ]
    findings = _findings_of(review_boq(lines), "capacity_inversion")
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "critical"
    assert f.line_no == 55  # the bigger-capacity (60 kva) line
    assert "55" in f.message_ar and "56" in f.message_ar


def test_capacity_inversion_unrelated_equipment_not_flagged():
    # Same capacity token, but clearly unrelated, dissimilar equipment types
    # (no shared distinctive vocabulary) -> the similarity heuristic must not
    # pair them up.
    lines = [
        Line(1, "لوحة كهرباء رئيسية للمبنى", spec="سعة 30 kva", unit_price=500),
        Line(2, "دهانات داخلية للحوائط والأسقف", spec="سماكة 60 kva", unit_price=100),
    ]
    assert _findings_of(review_boq(lines), "capacity_inversion") == []


def test_capacity_inversion_not_flagged_for_cable_with_ground_wire_clause():
    # "قطاع (4 × 70 + 35مم2 أرضي)" is a 70mm² cable with a 35mm² ground
    # wire, not a 35mm² cable. A naive "number immediately before مم" regex
    # grabs "35" here since it sits right next to the unit label, wrongly
    # reading this as smaller than the 50mm² line below and flagging a
    # spurious price inversion (this exact bug was found against the real
    # reference BoQ on lines 39/40 and must not return).
    lines = [
        Line(39, "كابل كهرباء", spec="قطاع (4× 50 مم2+25 أرضي)", unit_price=138),
        Line(40, "كابل كهرباء", spec="قطاع (4 × 70 + 35مم2 أرضي)", unit_price=165),
    ]
    assert _findings_of(review_boq(lines), "capacity_inversion") == []


# ---------------------------------------------------------------------------
# concentration_risk
# ---------------------------------------------------------------------------

def test_concentration_risk_not_flagged_when_spread_out():
    lines = [Line(i, f"بند {i}", spec="s", total=100) for i in range(1, 21)]
    assert _findings_of(review_boq(lines), "concentration_risk") == []


def test_concentration_risk_flagged_when_top10_dominate():
    big = [Line(i, f"بند كبير {i}", spec="s", total=1000) for i in range(1, 11)]
    small = [Line(i, f"بند صغير {i}", spec="s", total=1) for i in range(11, 31)]
    findings = _findings_of(review_boq(big + small), "concentration_risk")
    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert findings[0].line_no is None
    assert "%" in findings[0].message_ar


# ---------------------------------------------------------------------------
# vat_missing
# ---------------------------------------------------------------------------

def test_vat_mentioned_not_flagged():
    lines = [Line(1, "بند", spec="السعر لا يشمل ضريبة القيمة المضافة")]
    assert _findings_of(review_boq(lines), "vat_missing") == []


def test_vat_missing_flagged():
    lines = [Line(1, "بند", spec="مواصفات فنية عادية للباب الحديدي")]
    findings = _findings_of(review_boq(lines), "vat_missing")
    assert len(findings) == 1
    assert findings[0].severity == "info"
    assert findings[0].line_no is None


# ---------------------------------------------------------------------------
# REAL REFERENCE FILE TEST (T-BOQ-01 / T-BOQ-02 acceptance criteria)
#
# Uses the actual third-party hospital-rehab BoQ. The file itself is never
# copied into the repo or read into any file this test writes; it is only
# opened directly from its absolute path at test time and skipped if absent.
# ---------------------------------------------------------------------------

REFERENCE_BOQ_PATH = os.environ.get(
    "THAQIP_BOQ_FIXTURE",
    "/home/ais04/Downloads/TXC/إنشاءات عامة  المواد.xlsx",
)


class TestRealReferenceBoq:
    """T-BOQ-01 / T-BOQ-02: parse + review the real reference file."""

    @pytest.fixture(scope="class")
    def parsed(self):
        if not os.path.exists(REFERENCE_BOQ_PATH):
            pytest.skip(f"reference BoQ fixture not present at {REFERENCE_BOQ_PATH}")
        with open(REFERENCE_BOQ_PATH, "rb") as fh:
            data = fh.read()
        return parse_boq_xlsx(data)

    def test_parses_93_lines_and_grand_total(self, parsed):
        assert len(parsed.items) == 93
        grand_total = sum(
            (it.total for it in parsed.items if it.total is not None), Decimal(0)
        )
        assert abs(grand_total - Decimal("28669108")) <= 2

    def test_text_number_flags_at_least_55(self, parsed):
        # Observed on the real file: 63 lines have qty and/or unit_price
        # stored as text. Assert a safely-bounded floor rather than the exact
        # figure, per the task's guidance not to over-fit to one run.
        flagged = sum(1 for it in parsed.items if it.text_numbers)
        assert flagged >= 55

    def test_review_finds_unrounded_prices(self, parsed):
        findings = review_boq(parsed.items)
        unrounded = [f for f in findings if f.rule == "unrounded_unit_price"]
        assert len(unrounded) >= 3

    def test_review_finds_capacity_inversion_for_ups_pair(self, parsed):
        findings = review_boq(parsed.items)
        inversions = [f for f in findings if f.rule == "capacity_inversion"]
        assert len(inversions) >= 1
        # The true UPS pair (line 55: 60 kVA priced below line 56: 30 kVA)
        # must be among the findings, named by both line numbers.
        assert any("55" in f.message_ar and "56" in f.message_ar for f in inversions)

    def test_no_false_capacity_inversion_on_cable_ground_wire_spec(self, parsed):
        # Lines 39/40 are Gulf-style cable specs of the form
        # "قطاع (4 × N + K أرضي)". Line 40's main conductor is 70mm² (its
        # spec is "قطاع (4 × 70 + 35مم2 أرضي)") — the "35" is the ground
        # wire's size, not the item's own capacity — so it is legitimately
        # priced above line 39's 50mm² cable. A naive "number before مم"
        # regex previously misread line 40 as 35mm² and flagged a spurious
        # inversion against line 39; that must never come back.
        findings = review_boq(parsed.items)
        inversions = [f for f in findings if f.rule == "capacity_inversion"]
        assert not any(
            "39" in f.message_ar and "40" in f.message_ar for f in inversions
        )
        assert len(inversions) == 1

    def test_review_finds_concentration_risk_over_50pct(self, parsed):
        findings = review_boq(parsed.items)
        concentration = [f for f in findings if f.rule == "concentration_risk"]
        assert len(concentration) == 1
        msg = concentration[0].message_ar
        import re as _re
        m = _re.search(r"(\d+(?:\.\d+)?)%", msg)
        assert m is not None
        assert float(m.group(1)) >= 50
