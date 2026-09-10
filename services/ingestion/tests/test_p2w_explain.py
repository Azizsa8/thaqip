"""Tests for p2w.explain — the observed/derived/predicted separation.

The load-bearing test in this file is
``test_observed_never_contains_a_predicted_value``: the whole point of the module
is that a model output can never be shown to a user as a recorded fact, and that
property is checked structurally (no model keys, a real table + row id on every
entry) and by value (no observed number is the prediction's own quantile).

No database and no network: the connection is a fake that answers the three
queries the module issues.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from thaqip_ingestion.p2w import explain
from thaqip_ingestion.p2w.contracts import (
    MODEL_VERSION,
    EvidenceTier,
    ExplanationFactor,
    PredictionScope,
    PricePrediction,
    SuppressionReason,
)
from thaqip_ingestion.p2w.similarity import SimilarTender

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeConn:
    """Answers exactly the three queries explain.py issues, and nothing else."""

    def __init__(
        self,
        *,
        comparables: list[dict[str, Any]] | None = None,
        vendors: list[dict[str, Any]] | None = None,
        lineage: list[dict[str, Any]] | None = None,
        fail: bool = False,
    ) -> None:
        self.comparables = comparables or []
        self.vendors = vendors or []
        self.lineage = lineage or []
        self.fail = fail
        self.queries: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append(sql)
        if self.fail:
            raise RuntimeError("database is on fire")
        if "source_lineage" in sql:
            wanted = set(zip(args[0], args[1], strict=True))
            return [
                row
                for row in self.lineage
                if (row["fact_table"], row["fact_id"]) in wanted
            ]
        if "FROM tenders t" in sql:
            wanted = set(args[0])
            return [row for row in self.comparables if row["tender_id"] in wanted]
        if "FROM vendors v" in sql:
            wanted = set(args[0])
            return [row for row in self.vendors if row["id"] in wanted]
        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.queries.append(sql)
        if self.fail:
            raise RuntimeError("database is on fire")
        if "source_lineage" in sql:
            for row in self.lineage:
                if row["fact_table"] == args[0] and row["fact_id"] == args[1]:
                    return row
        return None


def make_similar(
    tender_id: int,
    *,
    award_value: float | None = 1_000_000.0,
    score: float = 0.8,
    age_days: float = 100.0,
    exclusion: str | None = None,
    name: str = "عقد صيانة",
) -> SimilarTender:
    return SimilarTender(
        tender_id=tender_id,
        name=name,
        agency="جهة حكومية",
        award_value=award_value,
        bidder_count=4,
        total_score=score,
        components={},
        exclusion_reason=exclusion,
        age_days=age_days,
    )


def comparable_row(
    tender_id: int, *, award_id: int | None = None, award_value: float | None = 1_000_000.0
) -> dict[str, Any]:
    return {
        "tender_id": tender_id,
        "tender_name": f"مشروع {tender_id}",
        "published_at": datetime(2025, 6, 1, tzinfo=UTC),
        "last_offer_date": datetime(2025, 7, 1, tzinfo=UTC),
        "offers_opening_date": datetime(2025, 7, 2, tzinfo=UTC),
        "award_id": award_id if award_id is not None else 900 + tender_id,
        "award_value": award_value,
        "awarded_at": datetime(2025, 8, 1, tzinfo=UTC),
    }


def market_prediction(
    *,
    p50: float = 1_000_000.0,
    tier: EvidenceTier = EvidenceTier.B,
    factors: list[ExplanationFactor] | None = None,
) -> PricePrediction:
    return PricePrediction(
        tender_id=1,
        prediction_scope=PredictionScope.MARKET,
        p10=p50 * 0.8,
        p50=p50,
        p90=p50 * 1.3,
        expected_value=p50,
        confidence_score=55,
        similarity_confidence=61,
        data_freshness_score=80,
        evidence_count=3,
        evidence_tier=tier,
        generated_at=AS_OF,
        explanation_factors=factors or [],
    )


def competitor_prediction(
    vendor_id: int, *, suppressed: bool = False, p50: float = 900_000.0
) -> PricePrediction:
    if suppressed:
        return PricePrediction.suppressed(
            tender_id=1,
            scope=PredictionScope.COMPETITOR,
            reason=SuppressionReason.INSUFFICIENT_EVIDENCE,
            subject_id=vendor_id,
            evidence_tier=EvidenceTier.C,
            generated_at=AS_OF,
        )
    return PricePrediction(
        tender_id=1,
        prediction_scope=PredictionScope.COMPETITOR,
        subject_id=vendor_id,
        p10=p50 * 0.85,
        p50=p50,
        p90=p50 * 1.2,
        confidence_score=48,
        evidence_count=9,
        evidence_tier=EvidenceTier.B,
        generated_at=AS_OF,
    )


# --------------------------------------------------------------------------
# the hard rule
# --------------------------------------------------------------------------


async def test_observed_never_contains_a_predicted_value():
    """No entry in `observed` may be, or look like, a model output."""
    prediction = market_prediction(p50=1_234_567.0)
    conn = FakeConn(comparables=[comparable_row(i, award_value=500_000.0 + i) for i in (11, 12)])

    result = await explain.build_explanation(
        conn,
        prediction=prediction,
        similar=[make_similar(11), make_similar(12)],
        competitors=[],
    )

    assert result["observed"], "expected observed facts for traceable comparables"
    predicted_numbers = {prediction.p10, prediction.p50, prediction.p90}
    for entry in result["observed"]:
        assert entry["kind"] == "observed"
        assert entry["source_table"] in explain.OBSERVED_TABLES
        assert isinstance(entry["source_id"], int)
        assert not {"p10", "p50", "p90", "confidence", "model_version"} & set(entry)
        assert entry["value"] not in predicted_numbers
    # And the prediction itself is present exactly once, in `predicted`.
    assert [e["p50"] for e in result["predicted"]] == [prediction.p50]


def test_guard_rejects_an_untraceable_observed_entry():
    with pytest.raises(ValueError, match="no traceable source_id"):
        explain._assert_observed_are_facts(
            [{"label": "x", "value": 1.0, "kind": "observed", "source_table": "awards"}]
        )


def test_guard_rejects_a_model_table_in_observed():
    with pytest.raises(ValueError, match="not an observed-fact table"):
        explain._assert_observed_are_facts(
            [
                {
                    "label": "x",
                    "value": 1.0,
                    "kind": "observed",
                    "source_table": "price_predictions",
                    "source_id": 4,
                }
            ]
        )


def test_guard_rejects_model_output_keys_in_observed():
    with pytest.raises(ValueError, match="model-output keys"):
        explain._assert_observed_are_facts(
            [
                {
                    "label": "x",
                    "value": 1.0,
                    "kind": "observed",
                    "source_table": "awards",
                    "source_id": 4,
                    "p50": 1.0,
                }
            ]
        )


def test_guard_rejects_a_wrong_kind():
    with pytest.raises(ValueError, match="must be kind='observed'"):
        explain._assert_observed_are_facts(
            [
                {
                    "label": "x",
                    "value": 1.0,
                    "kind": "predicted",
                    "source_table": "awards",
                    "source_id": 4,
                }
            ]
        )


# --------------------------------------------------------------------------
# observed facts
# --------------------------------------------------------------------------


async def test_every_observed_fact_cites_its_award_row():
    conn = FakeConn(comparables=[comparable_row(11, award_id=555)])
    result = await explain.build_explanation(
        conn, prediction=market_prediction(), similar=[make_similar(11)], competitors=[]
    )
    entry = result["observed"][0]
    assert entry["source_table"] == "awards"
    assert entry["source_id"] == 555
    assert entry["evidence_ref"] == "awards:555"
    assert entry["tender_id"] == 11
    assert entry["unit"] == explain.UNIT_SAR


async def test_untraceable_comparable_is_dropped_not_shown():
    """A comparable with no award row in the DB may not become an observation."""
    conn = FakeConn(comparables=[])  # the DB knows nothing about tender 11
    result = await explain.build_explanation(
        conn, prediction=market_prediction(), similar=[make_similar(11)], competitors=[]
    )
    assert result["observed"] == []
    assert any("قابلة للتتبع" in w or "قابل للتتبع" in w for w in result["data_quality_warnings"])


async def test_excluded_comparables_are_not_observed_but_are_counted():
    conn = FakeConn(comparables=[comparable_row(11), comparable_row(12)])
    result = await explain.build_explanation(
        conn,
        prediction=market_prediction(),
        similar=[make_similar(11), make_similar(12, exclusion="scale_incomparable")],
        competitors=[],
    )
    assert [e["tender_id"] for e in result["observed"]] == [11]
    excluded = next(d for d in result["derived"] if "المستبعدة" in d["label"])
    assert excluded["value"] == 1


async def test_comparable_without_award_value_is_not_observed():
    conn = FakeConn(comparables=[comparable_row(11, award_value=None)])
    result = await explain.build_explanation(
        conn,
        prediction=market_prediction(),
        similar=[make_similar(11, award_value=None)],
        competitors=[],
    )
    assert result["observed"] == []


async def test_observed_as_of_prefers_the_award_date():
    conn = FakeConn(comparables=[comparable_row(11)])
    result = await explain.build_explanation(
        conn, prediction=market_prediction(), similar=[make_similar(11)], competitors=[]
    )
    assert result["observed"][0]["as_of"] == "2025-08-01T00:00:00+00:00"


async def test_observed_entries_are_ordered_by_tender_id():
    conn = FakeConn(comparables=[comparable_row(i) for i in (30, 10, 20)])
    result = await explain.build_explanation(
        conn,
        prediction=market_prediction(),
        similar=[make_similar(30), make_similar(10), make_similar(20)],
        competitors=[],
    )
    assert [e["tender_id"] for e in result["observed"]] == [10, 20, 30]


async def test_vendor_identity_is_an_observed_fact():
    conn = FakeConn(
        comparables=[],
        vendors=[{"id": 7, "canonical_name": "شركة الإنشاء", "cr_number": "1010"}],
    )
    result = await explain.build_explanation(
        conn,
        prediction=market_prediction(),
        similar=[],
        competitors=[{"vendor_id": 7, "name": "شركة الإنشاء", "prediction": competitor_prediction(7)}],
    )
    vendor_entry = next(e for e in result["observed"] if e["source_table"] == "vendors")
    assert vendor_entry["source_id"] == 7
    assert vendor_entry["value"] == "شركة الإنشاء"
    assert vendor_entry["unit"] == explain.UNIT_NAME


# --------------------------------------------------------------------------
# lineage
# --------------------------------------------------------------------------


async def test_lineage_is_attached_when_a_row_exists():
    lineage = [
        {
            "fact_table": "awards",
            "fact_id": 911,
            "source_id": "etimad_visitor_api",
            "source_object_ref": "tender/911",
            "content_hash": "abc",
            "parser_version": "v3",
            "access_class": "PUBLIC_OPEN",
            "retrieved_at": datetime(2025, 8, 2, tzinfo=UTC),
            "source_name": "Etimad public visitor listing API",
            "registry_access_class": "PUBLIC_OPEN",
        }
    ]
    conn = FakeConn(comparables=[comparable_row(11, award_id=911)], lineage=lineage)
    result = await explain.build_explanation(
        conn, prediction=market_prediction(), similar=[make_similar(11)], competitors=[]
    )
    got = result["observed"][0]["lineage"]
    assert got["source_id"] == "etimad_visitor_api"
    assert got["parser_version"] == "v3"
    assert got["retrieved_at"] == "2025-08-02T00:00:00+00:00"


async def test_missing_lineage_is_reported_not_invented():
    conn = FakeConn(comparables=[comparable_row(11)])
    result = await explain.build_explanation(
        conn, prediction=market_prediction(), similar=[make_similar(11)], competitors=[]
    )
    assert result["observed"][0]["lineage"] is None
    assert any("source_lineage" in w for w in result["data_quality_warnings"])


async def test_trace_fact_returns_the_lineage_row():
    lineage = [
        {
            "fact_table": "awards",
            "fact_id": 911,
            "source_id": "forsah_public_api",
            "source_object_ref": None,
            "content_hash": None,
            "parser_version": "v1",
            "access_class": None,
            "retrieved_at": None,
            "source_name": "Forsah public opportunities API",
            "registry_access_class": "PUBLIC_OPEN",
        }
    ]
    conn = FakeConn(lineage=lineage)
    traced = await explain.trace_fact(conn, "awards", 911)
    assert traced["lineage_available"] is True
    assert traced["lineage"]["source_id"] == "forsah_public_api"
    # access_class falls back to the registry when the lineage row omits it.
    assert traced["lineage"]["access_class"] == "PUBLIC_OPEN"


async def test_trace_fact_says_so_when_nothing_is_recorded():
    traced = await explain.trace_fact(FakeConn(), "awards", 911)
    assert traced["lineage"] is None
    assert traced["lineage_available"] is False
    assert "no source_lineage row" in traced["note"]


async def test_trace_fact_survives_a_broken_database():
    traced = await explain.trace_fact(FakeConn(fail=True), "awards", 911)
    assert traced["lineage"] is None
    assert traced["lineage_available"] is False


async def test_trace_fact_refuses_a_non_observed_table():
    with pytest.raises(ValueError, match="fact_table must be one of"):
        await explain.trace_fact(FakeConn(), "price_predictions", 1)


async def test_trace_fact_refuses_a_non_integer_id():
    with pytest.raises(ValueError, match="fact_id must be an integer"):
        await explain.trace_fact(FakeConn(), "awards", "not-an-id")


# --------------------------------------------------------------------------
# derived / predicted / drivers
# --------------------------------------------------------------------------


async def test_derived_entries_all_name_a_method():
    conn = FakeConn(comparables=[comparable_row(11), comparable_row(12)])
    result = await explain.build_explanation(
        conn,
        prediction=market_prediction(),
        similar=[make_similar(11), make_similar(12)],
        competitors=[],
    )
    assert result["derived"]
    for entry in result["derived"]:
        assert set(entry) == {"label", "value", "method"}
        assert entry["method"]


async def test_derived_median_uses_observed_values_not_the_prediction():
    conn = FakeConn(
        comparables=[
            comparable_row(11, award_value=100.0),
            comparable_row(12, award_value=300.0),
        ]
    )
    result = await explain.build_explanation(
        conn,
        prediction=market_prediction(p50=999_999.0),
        similar=[make_similar(11), make_similar(12)],
        competitors=[],
    )
    median = next(d for d in result["derived"] if "الوسيط" in d["label"])
    assert median["value"] == 200.0


async def test_predicted_omits_suppressed_competitors():
    conn = FakeConn(vendors=[{"id": 7, "canonical_name": "أ", "cr_number": None}])
    result = await explain.build_explanation(
        conn,
        prediction=market_prediction(),
        similar=[],
        competitors=[
            {"vendor_id": 7, "name": "أ", "prediction": competitor_prediction(7, suppressed=True)},
            {"vendor_id": 8, "name": "ب", "prediction": competitor_prediction(8)},
        ],
    )
    subjects = [e["subject_id"] for e in result["predicted"]]
    assert subjects == [None, 8]
    assert any("محجوب" in w for w in result["data_quality_warnings"])


async def test_predicted_is_empty_for_a_suppressed_prediction():
    suppressed = PricePrediction.suppressed(
        tender_id=1,
        scope=PredictionScope.MARKET,
        reason=SuppressionReason.NO_COMPARABLE_TENDERS,
        evidence_tier=EvidenceTier.D,
        generated_at=AS_OF,
    )
    result = await explain.build_explanation(
        FakeConn(), prediction=suppressed, similar=[], competitors=[]
    )
    assert result["predicted"] == []
    assert result["suppression"]["reason"] == "NO_COMPARABLE_TENDERS"
    assert result["suppression"]["scope"] == "MARKET"


async def test_suppression_is_none_when_the_prediction_speaks():
    result = await explain.build_explanation(
        FakeConn(), prediction=market_prediction(), similar=[], competitors=[]
    )
    assert result["suppression"] is None


async def test_drivers_are_sorted_by_absolute_weight():
    factors = [
        ExplanationFactor(name="small", direction="increases", weight=0.1, kind="derived"),
        ExplanationFactor(name="big_negative", direction="decreases", weight=-0.9, kind="observed"),
        ExplanationFactor(name="medium", direction="increases", weight=0.5, kind="predicted"),
    ]
    result = await explain.build_explanation(
        FakeConn(),
        prediction=market_prediction(factors=factors),
        similar=[],
        competitors=[],
    )
    assert [d["name"] for d in result["drivers"]] == ["big_negative", "medium", "small"]
    assert [d["kind"] for d in result["drivers"]] == ["observed", "predicted", "derived"]


async def test_drivers_merge_competitor_factors_and_keep_scope():
    competitor = competitor_prediction(7)
    competitor.explanation_factors = [
        ExplanationFactor(name="vendor_history", direction="decreases", weight=2.0, kind="derived")
    ]
    result = await explain.build_explanation(
        FakeConn(vendors=[{"id": 7, "canonical_name": "أ", "cr_number": None}]),
        prediction=market_prediction(
            factors=[
                ExplanationFactor(name="sample", direction="neutral", weight=1.0, kind="derived")
            ]
        ),
        similar=[],
        competitors=[{"vendor_id": 7, "name": "أ", "prediction": competitor}],
    )
    top = result["drivers"][0]
    assert top["name"] == "vendor_history"
    assert top["scope"] == "COMPETITOR"
    assert top["subject_id"] == 7
    assert result["drivers"][1]["scope"] == "MARKET"


async def test_drivers_tie_break_on_name_is_deterministic():
    factors = [
        ExplanationFactor(name="zeta", direction="neutral", weight=1.0, kind="derived"),
        ExplanationFactor(name="alpha", direction="neutral", weight=-1.0, kind="derived"),
    ]
    result = await explain.build_explanation(
        FakeConn(), prediction=market_prediction(factors=factors), similar=[], competitors=[]
    )
    assert [d["name"] for d in result["drivers"]] == ["alpha", "zeta"]


# --------------------------------------------------------------------------
# warnings, inputs, robustness
# --------------------------------------------------------------------------


async def test_thin_sample_is_called_out():
    conn = FakeConn(comparables=[comparable_row(11)])
    result = await explain.build_explanation(
        conn, prediction=market_prediction(), similar=[make_similar(11)], competitors=[]
    )
    assert any("رقيقة" in w for w in result["data_quality_warnings"])


async def test_ageing_sample_is_called_out():
    conn = FakeConn(comparables=[comparable_row(i) for i in range(11, 19)])
    result = await explain.build_explanation(
        conn,
        prediction=market_prediction(),
        similar=[make_similar(i, age_days=800.0) for i in range(11, 19)],
        competitors=[],
    )
    assert any("عمر الأدلة" in w for w in result["data_quality_warnings"])


async def test_tier_c_warns_that_competitor_prices_are_barred():
    conn = FakeConn(comparables=[comparable_row(11)])
    result = await explain.build_explanation(
        conn,
        prediction=market_prediction(tier=EvidenceTier.C),
        similar=[make_similar(11)],
        competitors=[],
    )
    assert any("محجوبة" in w for w in result["data_quality_warnings"])


async def test_competitors_may_be_bare_predictions():
    conn = FakeConn(vendors=[{"id": 7, "canonical_name": "أ", "cr_number": None}])
    result = await explain.build_explanation(
        conn, prediction=market_prediction(), similar=[], competitors=[competitor_prediction(7)]
    )
    assert [e["subject_id"] for e in result["predicted"]] == [None, 7]


async def test_a_broken_database_yields_an_empty_but_valid_explanation():
    result = await explain.build_explanation(
        FakeConn(fail=True),
        prediction=market_prediction(),
        similar=[make_similar(11)],
        competitors=[],
    )
    assert result["observed"] == []
    assert result["data_quality_warnings"]
    assert result["model_version"] == MODEL_VERSION


async def test_a_non_prediction_argument_is_refused():
    with pytest.raises(ValueError, match="must be a PricePrediction"):
        await explain.build_explanation(
            FakeConn(), prediction={"p50": 1.0}, similar=[], competitors=[]
        )


async def test_output_shape_is_stable():
    result = await explain.build_explanation(
        FakeConn(), prediction=market_prediction(), similar=[], competitors=[]
    )
    assert set(result) == {
        "observed",
        "derived",
        "predicted",
        "drivers",
        "data_quality_warnings",
        "suppression",
        "model_version",
        "generated_at",
    }
    assert result["generated_at"] == AS_OF.isoformat()


def test_median_helper_handles_even_and_odd_and_empty():
    assert explain._median([]) is None
    assert explain._median([3.0]) == 3.0
    assert explain._median([1.0, 3.0]) == 2.0
    assert explain._median([5.0, 1.0, 3.0]) == 3.0
