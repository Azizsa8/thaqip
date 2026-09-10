"""P2W API contract tests.

The live tests need the real database (DATABASE_URL, default the local dev
postgres on 5433) and the p2w engine source tree; they skip rather than fail
when either is missing, so the pure-contract tests below still run anywhere.

Run:
    cd services/console
    DATABASE_URL=postgres://thaqip:thaqip_dev@localhost:5433/thaqip \
        .venv/bin/python -m pytest tests -q
"""
from __future__ import annotations

import json
import os

import pytest

from thaqip_console import auth as auth_mod
from thaqip_console.app import (
    LABEL_AR,
    LINEAGE_FACT_TABLES,
    P2W_CURRENCY,
    P2W_VAT_SEMANTICS,
    TENANT_HEADER,
    _amount,
    _observed,
    _prediction_envelope,
    _scenario_seed,
    app,
)

PRIVATE_FIELDS = ("estimated_cost", "min_margin_pct", "proposed_bid",
                  "risk_reserve", "target_win_pct")

MANDATORY_PREDICTION_KEYS = (
    "model_version", "generated_at", "confidence_score", "evidence_count",
    "evidence_tier", "suppression_reason",
)

DSN = os.environ.get("DATABASE_URL", "postgres://thaqip:thaqip_dev@localhost:5433/thaqip")


# --------------------------------------------------------------------------
# Pure contract tests — no database.
# --------------------------------------------------------------------------

def test_amount_always_carries_currency_and_vat_semantics():
    assert _amount(None) is None
    money = _amount(1234.567)
    assert money == {"amount": 1234.57, "currency": "SAR",
                     "vat_semantics": "unknown"}
    assert P2W_CURRENCY == "SAR"
    # We genuinely do not know whether published values include VAT. Claiming
    # either would be a fabricated fact.
    assert P2W_VAT_SEMANTICS == "unknown"


def test_observed_block_is_labelled_as_observed():
    block = _observed({"offers_seen": 3})
    assert block["kind"] == "observed"
    assert block["label_ar"] == LABEL_AR["observed"] == "مُلاحظ"
    assert block["offers_seen"] == 3


def _contracts():
    return pytest.importorskip("thaqip_ingestion.p2w.contracts")


def test_suppressed_prediction_envelope_carries_no_numbers():
    c = _contracts()
    pred = c.PricePrediction.suppressed(
        tender_id=7, scope=c.PredictionScope.COMPETITOR,
        reason=c.SuppressionReason.INSUFFICIENT_EVIDENCE,
        subject_id=42, evidence_count=1, evidence_tier=c.EvidenceTier.D)
    env = _prediction_envelope(pred, prediction_id=99)

    for key in MANDATORY_PREDICTION_KEYS:
        assert key in env, f"{key} missing from a suppressed envelope"
    assert env["is_suppressed"] is True
    assert env["suppression_reason"] == "INSUFFICIENT_EVIDENCE"
    assert env["quantiles"] is None
    assert env["expected_value"] is None
    assert env["win_probability"] is None
    assert env["kind"] == "predicted"
    assert env["label_ar"] == "متوقع"
    assert env["prediction_id"] == 99


def test_live_prediction_envelope_wraps_every_amount():
    c = _contracts()
    pred = c.PricePrediction(
        tender_id=7, prediction_scope=c.PredictionScope.MARKET,
        p10=10.0, p50=20.0, p90=30.0, expected_value=20.0,
        confidence_score=61, evidence_count=12, evidence_tier=c.EvidenceTier.B)
    env = _prediction_envelope(pred)

    for key in MANDATORY_PREDICTION_KEYS:
        assert key in env
    assert env["suppression_reason"] is None
    assert env["quantiles"]["p50"]["currency"] == "SAR"
    assert env["quantiles"]["p50"]["vat_semantics"] == "unknown"
    assert env["expected_value"]["amount"] == 20.0
    # confidence must never be readable as a win probability
    assert env["confidence_is_not_win_probability"] is True


def test_scenario_seed_is_deterministic_and_not_time_based():
    a = _scenario_seed(1668, 1, 1)
    b = _scenario_seed(1668, 1, 1)
    assert a == b, "the same (tender, tenant, version) must reproduce its seed"
    assert _scenario_seed(1668, 1, 2) != a, "a new version gets its own seed"
    assert _scenario_seed(1669, 1, 1) != a
    assert _scenario_seed(1668, 2, 1) != a
    assert isinstance(a, int) and a >= 0


def test_lineage_fact_tables_are_whitelisted():
    assert "tenders" in LINEAGE_FACT_TABLES
    for name in LINEAGE_FACT_TABLES:
        assert name.isidentifier(), "a fact table name reaches SQL; keep it inert"


# --------------------------------------------------------------------------
# Live tests — real database, real tender ids.
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    pytest.importorskip("thaqip_ingestion.p2w.contracts")
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    os.environ["DATABASE_URL"] = DSN
    # The auth gate refuses anonymous callers; act as a service token, the
    # one credential that may select a tenant with the header these tests use.
    auth_mod.SERVICE_TOKEN = "p2w-api-test-token"
    try:
        with fastapi_testclient.TestClient(
                app, headers={"Authorization": "Bearer p2w-api-test-token"}) as c:
            if c.get("/api/stats").status_code != 200:
                pytest.skip("console database is not reachable")
            yield c
    except OSError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")


@pytest.fixture(scope="module")
def tender_id(client):
    body = client.get("/api/tenders", params={"limit": 1, "open_only": True}).json()
    if not body["items"]:
        body = client.get("/api/tenders", params={"limit": 1}).json()
    if not body["items"]:
        pytest.skip("no tenders in the corpus")
    return body["items"][0]["id"]


def _assert_no_private_data(payload: object, where: str) -> None:
    """A shared endpoint must not leak tenant-private commercial inputs.

    Checked on the serialized response, so a private value nested anywhere —
    a key, a label, a free-text explanation — fails the test.
    """
    blob = json.dumps(payload, ensure_ascii=False)
    for field in PRIVATE_FIELDS:
        assert field not in blob, f"{where} leaked the private field {field!r}"


def test_market_intelligence_never_returns_tenant_private_inputs(client, tender_id):
    # Save a scenario first, so there IS private data in this tenant's rows
    # that the shared endpoint could leak.
    created = client.post(
        f"/api/tenders/{tender_id}/scenarios",
        json={"estimated_cost": 123456.78, "min_margin_pct": 9.5,
              "proposed_bid": 222222.22, "target_win_pct": 61.0,
              "risk_reserve": 3333.33, "name": "leak-probe"})
    assert created.status_code == 200, created.text

    r = client.get(f"/api/tenders/{tender_id}/market-intelligence")
    assert r.status_code == 200, r.text
    payload = r.json()
    _assert_no_private_data(payload, "market-intelligence")
    blob = json.dumps(payload, ensure_ascii=False)
    for value in ("123456.78", "222222.22", "3333.33"):
        assert value not in blob, f"market-intelligence leaked the value {value}"

    # observed facts and predictions live under separate keys, never merged
    assert payload["observed"]["kind"] == "observed"
    assert payload["prediction"]["kind"] == "predicted"
    assert "p50" not in payload["observed"]
    for key in MANDATORY_PREDICTION_KEYS:
        assert key in payload["prediction"]


def test_competitors_never_returns_tenant_private_inputs(client, tender_id):
    r = client.get(f"/api/tenders/{tender_id}/competitors", params={"limit": 3})
    assert r.status_code == 200, r.text
    payload = r.json()
    _assert_no_private_data(payload, "competitors")

    for candidate in payload["candidates"]:
        pred = candidate["price_prediction"]
        for key in MANDATORY_PREDICTION_KEYS:
            assert key in pred
        if pred["is_suppressed"]:
            # Suppressed rows are returned, not dropped, and carry no numbers.
            assert pred["suppression_reason"]
            assert pred["quantiles"] is None
        assert candidate["observed"]["kind"] == "observed"


def test_unknown_tenant_is_refused_rather_than_served_the_default(client):
    r = client.get("/api/pursuits", headers={TENANT_HEADER: "no-such-tenant"})
    assert r.status_code == 404


def test_scenario_curve_is_tenant_scoped_and_reproducible(client, tender_id):
    created = client.post(
        f"/api/tenders/{tender_id}/scenarios",
        json={"estimated_cost": 50000, "min_margin_pct": 10, "name": "curve-probe"})
    assert created.status_code == 200, created.text
    scenario = created.json()["scenario"]
    assert scenario["seed"] == _scenario_seed(
        tender_id, scenario["tenant_id"], scenario["version"])

    first = client.get(f"/api/scenarios/{scenario['id']}/curve")
    assert first.status_code == 200, first.text
    assert first.json()["cache"] == "miss"
    again = client.get(f"/api/scenarios/{scenario['id']}/curve", params={"refresh": True})
    assert again.status_code == 200
    # Same inputs, same model_version, same seed => same curve.
    assert again.json()["curve"] == first.json()["curve"]

    # Another tenant must not be able to read it. Only the default tenant is
    # provisioned, so an unknown slug is refused at the door.
    other = client.get(f"/api/scenarios/{scenario['id']}/curve",
                       headers={TENANT_HEADER: "no-such-tenant"})
    assert other.status_code == 404


def test_lineage_rejects_an_unlisted_fact_table(client, tender_id):
    assert client.get("/api/internal/lineage/pg_shadow/1").status_code == 422
    ok = client.get(f"/api/internal/lineage/tenders/{tender_id}")
    assert ok.status_code == 200
    body = ok.json()
    assert body["fact_table"] == "tenders"
    # Nothing is claimed as traced when no lineage rows exist.
    assert isinstance(body["traceable"], bool)
    if not body["traceable"]:
        assert body["lineage"] == [] and body["note"]


def test_readiness_reports_unmeasurable_dimensions_as_null(client):
    r = client.get("/api/readiness", params={"sample": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["domains"]) == {
        "data_readiness", "model_readiness", "product_ux_readiness",
        "security_privacy_legal", "operational_readiness", "commercial_readiness"}
    for name, domain in body["domains"].items():
        for key, dim in domain["dimensions"].items():
            if dim["measured"]:
                assert dim["score"] is not None, f"{name}.{key}"
            else:
                assert dim["score"] is None, f"{name}.{key}"
                assert dim["not_measurable_reason"], f"{name}.{key} needs a reason"
        assert 0.0 <= domain["measured_weight_pct"] <= 100.0
    assert body["overall_weight_covered_pct"] <= 100.0
