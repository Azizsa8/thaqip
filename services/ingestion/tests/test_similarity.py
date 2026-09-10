"""Tests for p2w.similarity — deterministic, no network, no database writes."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from thaqip_ingestion.p2w import similarity as sim

# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeConn:
    """Minimal asyncpg-shaped stand-in.  Returns a fixed row list from fetch()."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.fetch_calls: list[tuple[Any, ...]] = []
        self.executemany_calls: list[tuple[str, list[tuple[Any, ...]]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append(args)
        return self.rows

    async def executemany(self, sql: str, payload: list[tuple[Any, ...]]) -> None:
        self.executemany_calls.append((sql, list(payload)))


def make_row(**over: Any) -> dict[str, Any]:
    """A candidate row with every column find_similar_tenders reads."""
    row: dict[str, Any] = {
        "id": 100,
        "name": "صيانة وتشغيل المكيفات",
        "agency_id": 7,
        "agency_name_raw": "ادارة الشؤون الفنية المركزية بمنطقة الرياض",
        "branch_name": "إدارة المشتريات",
        "activity_id": 111,
        "activity_name_raw": "خدمات الصيانة والتشغيل",
        "booklet_price": 200.0,
        "published_at": datetime(2025, 1, 1, tzinfo=UTC),
        "last_offer_date": datetime(2025, 1, 21, tzinfo=UTC),
        "offers_opening_date": datetime(2025, 1, 22, tzinfo=UTC),
        "award_value": 1_000_000.0,
        "bidder_count": 4,
    }
    row.update(over)
    return row


AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# weights
# --------------------------------------------------------------------------


def test_weights_sum_to_one_and_cover_the_six_components() -> None:
    assert set(sim.SIMILARITY_WEIGHTS) == {
        "activity", "agency", "scale", "region", "duration", "semantic",
    }
    assert math.isclose(sum(sim.SIMILARITY_WEIGHTS.values()), 1.0, abs_tol=1e-9)
    assert all(w > 0 for w in sim.SIMILARITY_WEIGHTS.values())


def test_total_score_is_bounded_by_zero_and_one() -> None:
    best = sim.score_candidate(
        make_row(id=1, agency_id=7, activity_id=111),
        make_row(id=2, agency_id=7, activity_id=111),
        as_of=AS_OF,
    )
    assert 0.0 <= best.total_score <= 1.0
    assert math.isclose(best.total_score, 1.0, abs_tol=1e-9)


# --------------------------------------------------------------------------
# scale
# --------------------------------------------------------------------------


def test_scale_similarity_is_one_for_equal_values() -> None:
    assert sim.scale_similarity(500_000, 500_000) == 1.0


def test_scale_similarity_is_symmetric() -> None:
    for a, b in ((1_000.0, 7_300.0), (250_000.0, 1_100_000.0), (3.0, 91.0)):
        assert math.isclose(
            sim.scale_similarity(a, b), sim.scale_similarity(b, a), abs_tol=1e-12
        )


def test_scale_similarity_is_zero_for_an_order_of_magnitude_gap() -> None:
    assert math.isclose(sim.scale_similarity(100_000, 1_000_000), 0.0, abs_tol=1e-12)
    assert math.isclose(sim.scale_similarity(1_000_000, 100_000), 0.0, abs_tol=1e-12)
    # beyond 10x it clamps at 0 rather than going negative
    assert sim.scale_similarity(1_000, 10_000_000) == 0.0


def test_scale_similarity_returns_none_when_uncomputable() -> None:
    assert sim.scale_similarity(None, 5.0) is None
    assert sim.scale_similarity(5.0, None) is None
    assert sim.scale_similarity(0.0, 5.0) is None
    assert sim.scale_similarity(-3.0, 5.0) is None


def test_scale_falls_back_to_booklet_price_then_to_neutral_with_a_note() -> None:
    subject = make_row(id=1, award_value=None, booklet_price=200.0)
    candidate = make_row(id=2, award_value=500_000.0, booklet_price=200.0)
    scored = sim.score_candidate(subject, candidate, as_of=AS_OF)
    assert scored.components["scale"] == 1.0  # booklet prices are equal

    subject_no_scale = make_row(id=1, award_value=None, booklet_price=0.0)
    candidate_zero = make_row(id=2, award_value=900_000.0, booklet_price=0.0)
    neutral = sim.score_candidate(subject_no_scale, candidate_zero, as_of=AS_OF)
    assert neutral.components["scale"] == sim.NEUTRAL_SCORE
    assert any(n.startswith("scale_neutral:") for n in neutral.notes)


def test_scale_incomparable_exclusion() -> None:
    subject = make_row(id=1, award_value=100_000.0)
    candidate = make_row(id=2, award_value=50_000_000.0)
    scored = sim.score_candidate(subject, candidate, as_of=AS_OF)
    assert scored.components["scale"] < sim.SCALE_INCOMPARABLE_THRESHOLD
    assert scored.exclusion_reason == sim.EXCLUSION_SCALE


# --------------------------------------------------------------------------
# point-in-time
# --------------------------------------------------------------------------


def test_point_in_time_precedence() -> None:
    row = make_row()
    assert sim.point_in_time(row) == datetime(2025, 1, 22, tzinfo=UTC)
    assert sim.point_in_time(make_row(offers_opening_date=None)) == datetime(
        2025, 1, 21, tzinfo=UTC
    )
    assert sim.point_in_time(
        make_row(offers_opening_date=None, last_offer_date=None)
    ) == datetime(2025, 1, 1, tzinfo=UTC)
    assert sim.point_in_time(
        make_row(offers_opening_date=None, last_offer_date=None, published_at=None)
    ) is None


def test_point_in_time_treats_naive_datetimes_as_utc() -> None:
    naive = sim.point_in_time(make_row(offers_opening_date=datetime(2025, 6, 1)))  # noqa: DTZ001 - naive on purpose
    assert naive == datetime(2025, 6, 1, tzinfo=UTC)


async def test_future_candidates_are_excluded_and_never_returned_by_default() -> None:
    past = make_row(id=10, offers_opening_date=datetime(2025, 6, 1, tzinfo=UTC))
    same_instant = make_row(id=11, offers_opening_date=AS_OF)  # strictly-before gate
    future = make_row(id=12, offers_opening_date=datetime(2026, 6, 1, tzinfo=UTC))
    conn = FakeConn([past, same_instant, future])
    subject = make_row(id=1, award_value=1_000_000.0)

    kept = await sim.find_similar_tenders(conn, tender=subject, as_of=AS_OF)
    assert [r.tender_id for r in kept] == [10]

    everything = await sim.find_similar_tenders(
        conn, tender=subject, as_of=AS_OF, include_excluded=True
    )
    by_id = {r.tender_id: r for r in everything}
    assert by_id[11].exclusion_reason == sim.EXCLUSION_FUTURE
    assert by_id[12].exclusion_reason == sim.EXCLUSION_FUTURE
    assert by_id[10].exclusion_reason is None


async def test_candidate_without_any_timestamp_is_excluded_as_unprovable() -> None:
    undated = make_row(
        id=20, offers_opening_date=None, last_offer_date=None, published_at=None
    )
    conn = FakeConn([undated])
    result = await sim.find_similar_tenders(
        conn, tender=make_row(id=1), as_of=AS_OF, include_excluded=True
    )
    assert result[0].exclusion_reason == sim.EXCLUSION_FUTURE
    assert "no_point_in_time_timestamp" in result[0].notes
    assert math.isnan(result[0].age_days)


async def test_as_of_defaults_to_the_subject_tenders_own_point_in_time() -> None:
    subject = make_row(id=1, offers_opening_date=datetime(2025, 3, 1, tzinfo=UTC))
    before = make_row(id=30, offers_opening_date=datetime(2025, 2, 1, tzinfo=UTC))
    after = make_row(id=31, offers_opening_date=datetime(2025, 4, 1, tzinfo=UTC))
    conn = FakeConn([before, after])
    kept = await sim.find_similar_tenders(conn, tender=subject)
    assert [r.tender_id for r in kept] == [30]


def test_age_days_is_measured_from_as_of() -> None:
    candidate = make_row(id=2, offers_opening_date=AS_OF - timedelta(days=30))
    scored = sim.score_candidate(make_row(id=1), candidate, as_of=AS_OF)
    assert scored.age_days == pytest.approx(30.0)


# --------------------------------------------------------------------------
# other exclusions
# --------------------------------------------------------------------------


def test_self_is_excluded() -> None:
    row = make_row(id=42)
    scored = sim.score_candidate(row, dict(row), as_of=AS_OF)
    assert scored.exclusion_reason == sim.EXCLUSION_SELF


def test_missing_award_value_is_excluded() -> None:
    scored = sim.score_candidate(
        make_row(id=1), make_row(id=2, award_value=None), as_of=AS_OF
    )
    assert scored.exclusion_reason == sim.EXCLUSION_NO_AWARD_VALUE


def test_point_in_time_exclusion_outranks_missing_award_value() -> None:
    """A leak is the more serious failure, so the future reason must win."""
    scored = sim.score_candidate(
        make_row(id=1),
        make_row(id=2, award_value=None, offers_opening_date=AS_OF + timedelta(days=1)),
        as_of=AS_OF,
    )
    assert scored.exclusion_reason == sim.EXCLUSION_FUTURE


def test_every_exclusion_reason_is_in_the_declared_set() -> None:
    rows = [
        make_row(id=1),
        make_row(id=2, award_value=None),
        make_row(id=3, award_value=99_000_000_000.0),
        make_row(id=4, offers_opening_date=AS_OF + timedelta(days=1)),
    ]
    for row in rows:
        scored = sim.score_candidate(make_row(id=1), row, as_of=AS_OF)
        assert scored.exclusion_reason in (None,) + sim.EXCLUSION_REASONS


# --------------------------------------------------------------------------
# arabic normalization / semantic
# --------------------------------------------------------------------------


def test_arabic_normalization_matches_orthographic_variants() -> None:
    # taa marbuta -> haa, tatweel stripped
    assert sim.semantic_similarity(
        "صيانة وتشغيل المكيفات", "صيانه وتشغيـل المكيفات"
    ) == 1.0
    # hamza forms -> alef, alef maqsura -> yaa
    assert sim.semantic_similarity("إنشاء مبنى إداري", "انشاء مبني اداري") == 1.0
    # diacritics stripped
    assert sim.semantic_similarity("تَوْرِيد أَجْهِزَة", "توريد اجهزه") == 1.0


def test_semantic_similarity_separates_unrelated_names() -> None:
    assert sim.semantic_similarity("توريد أجهزة حاسب", "إنشاء طريق دائري") == 0.0
    partial = sim.semantic_similarity("صيانة المكيفات", "صيانة المصاعد")
    assert 0.0 < partial < 1.0


def test_normalized_tokens_drops_generic_stopwords() -> None:
    tokens = sim.normalized_tokens("منافسة رقم 123 صيانة المكيفات")
    assert "صيانه" in tokens
    assert sim.normalize_ar("منافسة") not in tokens
    assert sim.normalize_ar("رقم") not in tokens


def test_token_jaccard_edges() -> None:
    assert sim.token_jaccard([], ["a"]) == 0.0
    assert sim.token_jaccard(["a"], []) == 0.0
    assert sim.token_jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)


# --------------------------------------------------------------------------
# activity / agency / region / duration
# --------------------------------------------------------------------------


def test_activity_match_exact_related_and_unrelated() -> None:
    assert sim.activity_match(111, 111) == 1.0
    assert sim.activity_match(
        111, 222, "خدمات الصيانة والتشغيل", "خدمات الصيانة العامة"
    ) == sim.ACTIVITY_RELATED_SCORE
    assert sim.activity_match(111, 222, "أعمال الطرق", "توريد أجهزة طبية") == 0.0
    assert sim.activity_match(None, None, None, None) == 0.0


def test_agency_match_requires_resolved_ids() -> None:
    assert sim.agency_match(7, 7) == 1.0
    assert sim.agency_match(7, 8) == 0.0
    assert sim.agency_match(None, 7) == 0.0
    assert sim.agency_match(None, None) == 0.0


def test_region_of_reads_real_agency_strings() -> None:
    # exact strings taken from the live tenders table
    assert sim.region_of("ادارة الشؤون الفنية المركزية بمنطقة المدينةالمنورة") == "madinah"
    assert sim.region_of("ادارة الشؤون الفنية المركزية بمنطقة عسير") == "asir"
    assert sim.region_of("ادارة الشؤون الفنية المركزية بالمنطقة الشرقية") == "eastern"
    assert sim.region_of(None, "إدارة المشتريات - مدينة الملك عبدالعزيز الطبية بالرياض") == "riyadh"
    assert sim.region_of("جهة حكومية بدون منطقة") is None
    assert sim.region_of(None, None) is None


def test_region_match_scores_unknown_as_neutral_not_as_mismatch() -> None:
    assert sim.region_match("riyadh", "riyadh") == 1.0
    assert sim.region_match("riyadh", "asir") == 0.0
    assert sim.region_match(None, "asir") == sim.NEUTRAL_SCORE
    assert sim.region_match("asir", None) == sim.NEUTRAL_SCORE


def test_duration_similarity_linear_and_neutral_when_missing() -> None:
    assert sim.duration_similarity(30.0, 30.0) == 1.0
    assert sim.duration_similarity(30.0, 30.0 + sim.DURATION_SPAN_DAYS) == 0.0
    assert sim.duration_similarity(20.0, 50.0) == pytest.approx(0.5)
    assert sim.duration_similarity(None, 20.0) is None
    scored = sim.score_candidate(
        make_row(id=1, published_at=None), make_row(id=2), as_of=AS_OF
    )
    assert scored.components["duration"] == sim.NEUTRAL_SCORE
    assert "duration_neutral:missing_dates" in scored.notes


# --------------------------------------------------------------------------
# retrieval behaviour
# --------------------------------------------------------------------------


async def test_results_are_ranked_and_limited_deterministically() -> None:
    subject = make_row(id=1, activity_id=111, agency_id=7, award_value=1_000_000.0)
    rows = [
        make_row(id=50, activity_id=111, agency_id=7),                    # best
        make_row(id=51, activity_id=999, agency_id=8, name="إنشاء طريق"),  # worst
        make_row(id=52, activity_id=111, agency_id=8),                    # middle
    ]
    conn = FakeConn(rows)
    first = await sim.find_similar_tenders(conn, tender=subject, as_of=AS_OF)
    second = await sim.find_similar_tenders(conn, tender=subject, as_of=AS_OF)
    assert [r.tender_id for r in first] == [50, 52, 51]
    assert [r.to_dict() for r in first] == [r.to_dict() for r in second]

    capped = await sim.find_similar_tenders(conn, tender=subject, as_of=AS_OF, limit=2)
    assert [r.tender_id for r in capped] == [50, 52]


async def test_ties_break_on_tender_id_for_determinism() -> None:
    rows = [make_row(id=71), make_row(id=70), make_row(id=72)]
    conn = FakeConn(rows)
    out = await sim.find_similar_tenders(conn, tender=make_row(id=1), as_of=AS_OF)
    assert len({r.total_score for r in out}) == 1
    assert [r.tender_id for r in out] == [70, 71, 72]


async def test_include_excluded_appends_excluded_rows_after_eligible_ones() -> None:
    conn = FakeConn([
        make_row(id=80),
        make_row(id=81, award_value=None),
        make_row(id=82, offers_opening_date=AS_OF + timedelta(days=5)),
    ])
    out = await sim.find_similar_tenders(
        conn, tender=make_row(id=1), as_of=AS_OF, include_excluded=True
    )
    assert [r.tender_id for r in out[:1]] == [80]
    assert all(r.is_excluded for r in out[1:])
    assert {r.tender_id for r in out[1:]} == {81, 82}


async def test_query_is_parameterised_with_the_subject_keys() -> None:
    conn = FakeConn([])
    await sim.find_similar_tenders(
        conn, tender=make_row(id=5, activity_id=111, agency_id=7), as_of=AS_OF
    )
    assert conn.fetch_calls == [(5, 111, 7, sim.CANDIDATE_POOL_LIMIT)]


def test_post_award_bidder_count_is_carried_but_never_scored() -> None:
    a = sim.score_candidate(make_row(id=1), make_row(id=2, bidder_count=1), as_of=AS_OF)
    b = sim.score_candidate(make_row(id=1), make_row(id=2, bidder_count=17), as_of=AS_OF)
    assert a.total_score == b.total_score
    assert a.components == b.components
    assert (a.bidder_count, b.bidder_count) == (1, 17)


def test_components_are_all_present_and_in_range() -> None:
    scored = sim.score_candidate(make_row(id=1), make_row(id=2), as_of=AS_OF)
    assert set(scored.components) == set(sim.SIMILARITY_WEIGHTS)
    assert all(0.0 <= v <= 1.0 for v in scored.components.values())
    payload = scored.to_dict()
    assert payload["retrieval_version"] == sim.RETRIEVAL_VERSION
    assert payload["components"] == scored.components


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


async def test_persist_similarity_writes_rows_and_skips_self() -> None:
    conn = FakeConn([])
    results = [
        sim.score_candidate(make_row(id=1), make_row(id=2), as_of=AS_OF),
        sim.score_candidate(make_row(id=1), make_row(id=1), as_of=AS_OF),  # self
        sim.score_candidate(
            make_row(id=1), make_row(id=3, award_value=None), as_of=AS_OF
        ),
    ]
    written = await sim.persist_similarity(conn, 1, results)
    assert written == 2
    _sql, payload = conn.executemany_calls[0]
    assert [row[1] for row in payload] == [2, 3]
    assert all(row[0] == 1 for row in payload)
    assert all(row[5] == sim.RETRIEVAL_VERSION for row in payload)
    # the excluded row is persisted WITH its reason (it is part of the evidence trail)
    assert payload[1][4] == sim.EXCLUSION_NO_AWARD_VALUE
    # components serialise as json text for the ::jsonb cast
    assert payload[0][3].startswith("{")


async def test_persist_similarity_is_a_noop_without_rows() -> None:
    conn = FakeConn([])
    assert await sim.persist_similarity(conn, 1, []) == 0
    assert conn.executemany_calls == []
