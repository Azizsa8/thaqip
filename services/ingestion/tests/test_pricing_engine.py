from unittest.mock import AsyncMock

import pytest

from thaqip_ingestion.pricing_engine import calculate_price_simulation, classify_zone


def test_classify_zone():
    assert classify_zone(800, median=1000, p25=850, p75=1200) == "aggressive"
    assert classify_zone(950, median=1000, p25=850, p75=1200) == "sweet_spot"
    assert classify_zone(1100, median=1000, p25=850, p75=1200) == "conservative"
    assert classify_zone(1300, median=1000, p25=850, p75=1200) == "uncompetitive"
    assert classify_zone(1000, None, None, None) == "sweet_spot"


@pytest.mark.asyncio
async def test_calculate_price_simulation_with_benchmarks():
    mock_conn = AsyncMock()
    # Mock benchmark row: median 1,000,000 SAR
    mock_conn.fetchrow.side_effect = [
        {
            "n": 25,
            "min_val": 600000.0,
            "p25_val": 850000.0,
            "p50_val": 1000000.0,
            "p75_val": 1250000.0,
            "max_val": 1800000.0,
        },
        {"median_bidders": 4.0},
    ]

    tender = {"id": 1, "activity_id": 10, "agency_id": 2, "source": "etimad"}
    
    # 1. Price in sweet spot: 900,000 SAR
    res = await calculate_price_simulation(mock_conn, tender=tender, proposed_price=900000.0)
    assert res.proposed_price == 900000.0
    assert res.competitive_zone == "sweet_spot"
    assert res.win_probability_pct > 50.0
    assert res.gtpl_abnormally_low_flag is False
    assert res.expected_value > 0

    # 2. Abnormally low tender: 650,000 SAR (< 70% of median 1,000,000)
    mock_conn.fetchrow.side_effect = [
        {
            "n": 25,
            "min_val": 600000.0,
            "p25_val": 850000.0,
            "p50_val": 1000000.0,
            "p75_val": 1250000.0,
            "max_val": 1800000.0,
        },
        {"median_bidders": 4.0},
    ]
    res_low = await calculate_price_simulation(mock_conn, tender=tender, proposed_price=650000.0)
    assert res_low.gtpl_abnormally_low_flag is True
    assert res_low.competitive_zone == "aggressive"


@pytest.mark.asyncio
async def test_calculate_price_simulation_fallback():
    mock_conn = AsyncMock()
    # No historical awards
    mock_conn.fetchrow.side_effect = [
        {"n": 0, "min_val": None, "p25_val": None, "p50_val": None, "p75_val": None, "max_val": None},
        {"median_bidders": None},
    ]

    tender = {"id": 2, "activity_id": 999, "source": "etimad"}
    res = await calculate_price_simulation(mock_conn, tender=tender, proposed_price=500000.0)
    assert res.basis == "density_fallback"
    assert res.win_probability_pct == 20.0  # 1 / (4 + 1)
    assert res.gtpl_abnormally_low_flag is False
