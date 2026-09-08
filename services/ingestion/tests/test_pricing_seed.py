from thaqip_ingestion.pricing_seed import _baseline_price, _win_probability_pct


def test_baseline_price_prefers_activity_history():
    assert _baseline_price(median_award=50_000, booklet_price=500) == (
        46_000,
        "baseline_activity_history",
    )


def test_baseline_price_uses_booklet_floor_when_history_missing():
    assert _baseline_price(median_award=None, booklet_price=50) == (
        10_000,
        "baseline_booklet_price",
    )
    assert _baseline_price(median_award=None, booklet_price=1_000) == (
        120_000,
        "baseline_booklet_price",
    )


def test_baseline_price_default_when_no_signal():
    assert _baseline_price(median_award=None, booklet_price=None) == (
        100_000,
        "baseline_default",
    )


def test_win_probability_uses_market_median_when_available():
    lower_than_median = _win_probability_pct(
        proposed_price=92_000,
        median_award=100_000,
        live_bidders=20,
    )
    above_median = _win_probability_pct(
        proposed_price=120_000,
        median_award=100_000,
        live_bidders=20,
    )
    assert lower_than_median > above_median
    assert 2 <= above_median <= 95


def test_win_probability_falls_back_to_bidder_density():
    assert _win_probability_pct(proposed_price=100_000, median_award=None, live_bidders=4) == 20.0
