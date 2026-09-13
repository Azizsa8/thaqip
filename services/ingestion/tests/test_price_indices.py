"""GASTAT index time adjustment: point-in-time correctness and fallbacks."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from thaqip_ingestion import price_indices
from thaqip_ingestion.p2w import market
from thaqip_ingestion.p2w.indices import IndexSeries, published_by

CPI = IndexSeries.from_rows("cpi.general", [
    (date(2025, 6, 1), 100.0),
    (date(2025, 7, 1), 101.0),
    (date(2026, 5, 1), 104.0),
    (date(2026, 6, 1), 105.0),   # published 15 Aug 2026
    (date(2026, 7, 1), 110.0),   # published 15 Sep 2026
])


def test_a_month_is_usable_only_after_its_publication_lag():
    assert published_by(date(2026, 6, 1)) == date(2026, 8, 15)
    assert CPI.latest_published(date(2026, 8, 14)) == date(2026, 5, 1)
    assert CPI.latest_published(date(2026, 8, 20)) == date(2026, 6, 1)
    assert CPI.latest_published(date(2026, 9, 20)) == date(2026, 7, 1)


def test_backtests_cannot_see_index_months_published_after_as_of():
    """On 1 Sep the table may already hold July (out mid-September); the
    restatement must be identical to one computed without July."""
    as_of = datetime(2026, 9, 1, tzinfo=UTC)
    without_july = IndexSeries.from_rows("cpi.general", CPI.points[:-1])
    age = (as_of.date() - date(2025, 7, 15)).days
    got, latest = CPI.factor(age, as_of)
    assert latest == date(2026, 6, 1)
    assert got == pytest.approx(without_july.factor(age, as_of)[0])


def test_levels_interpolate_inside_published_months():
    mid_june, mid_july = date(2025, 6, 15), date(2025, 7, 15)
    halfway = mid_june + (mid_july - mid_june) / 2
    assert CPI.level(halfway, date(2026, 9, 20)) == pytest.approx(100.5, abs=0.02)


def test_after_the_last_release_the_level_is_nowcast_with_the_trailing_year():
    as_of = date(2026, 8, 20)                 # latest published: June 2026 = 105
    annual = 105.0 / 100.0                    # June 2026 over June 2025
    years = (as_of - date(2026, 6, 15)).days / 365.25
    assert CPI.level(as_of, as_of) == pytest.approx(105.0 * annual ** years)


def test_recent_awards_get_a_real_factor_not_exactly_one():
    """Awards after the last release used to be restated by exactly 1.0, which
    let a predicted quantile coincide with an observed award value."""
    factor, _ = CPI.factor(20.0, datetime(2026, 8, 20, tzinfo=UTC))
    assert factor > 1.0


def test_dates_before_the_series_start_are_not_extrapolated():
    assert CPI.factor(3 * 365.0, datetime(2026, 9, 20, tzinfo=UTC)) is None


def test_index_can_restate_downward_unlike_the_flat_model():
    falling = IndexSeries.from_rows("cpi.general", [(date(2018, 12, 1), 94.5), (date(2019, 6, 1), 90.0)])
    factor, _ = falling.factor(260.0, datetime(2019, 9, 1, tzinfo=UTC))  # award mid-Dec 2018
    assert factor < 1.0


def _comparable(tid, value, age):
    return {"tender_id": tid, "name": f"t{tid}", "award_value": value, "total_score": 0.9,
            "age_days": age, "exclusion_reason": None}


def test_build_sample_uses_the_index_and_falls_back_per_comparable():
    as_of = datetime(2026, 9, 20, tzinfo=UTC)
    sample = market.build_sample(
        [_comparable(1, 100_000, (as_of.date() - date(2025, 7, 15)).days),
         _comparable(2, 100_000, 6 * 365.0)],           # before the series: flat
        index=CPI, as_of=as_of)
    by_id = {s.tender_id: s for s in sample}
    assert by_id[1].adjustment == "index:cpi.general"
    expected = CPI.factor((as_of.date() - date(2025, 7, 15)).days, as_of)[0]
    assert by_id[1].adjusted_value == pytest.approx(100_000 * expected)
    assert by_id[2].adjustment == "flat"
    assert by_id[2].adjusted_value == pytest.approx(100_000 * market.inflation_factor(6 * 365.0))
    factor = market._time_adjustment_factor(sample, annual_rate=0.02, median_factor=1.05)
    assert "الهيئة العامة للإحصاء" in factor.detail and "خارج نطاق المؤشر" in factor.detail


def test_without_an_index_the_model_is_exactly_the_old_flat_one():
    sample = market.build_sample([_comparable(1, 50_000, 730.0)])
    assert sample[0].adjustment == "flat"
    assert sample[0].adjusted_value == pytest.approx(50_000 * 1.02 ** (730 / market.DAYS_PER_YEAR))


def test_fetcher_extracts_only_the_requested_member_and_skips_bad_rows():
    spec = next(s for s in price_indices.SERIES if s.key == "wpi.metal_machinery")
    payload = {"data": [
        {"Month": "2026-07", "Category": "Metal products, machinery and equipment", "Wholesale Price Index": 137.6},
        {"Month": "2026-06", "Category": "Metal products, machinery and equipment", "Wholesale Price Index": None},
        {"Month": "2026-07", "Category": "General index", "Wholesale Price Index": 164.0},
        {"Month": "2026-05", "Category": "Metal products, machinery and equipment", "Wholesale Price Index": 0},
    ]}
    assert price_indices.extract(spec, payload) == [(date(2026, 7, 1), 137.6)]
