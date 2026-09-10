"""API contract, suppression and localization battery (testing guide 15 + 11).

Drives the LIVE console at ``localhost:8091``. Everything here is an assertion
about the wire format a browser actually receives, which is the only place the
house rules can be broken in a way a unit test cannot see: the engine modules
are all pure and well covered, but the shape the console composes out of them
is what ships.

If the console is not running the whole module skips rather than failing — a
red bar that only means "docker is down" trains people to ignore red bars.
"""

from __future__ import annotations

import os
import statistics
import time
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("THAQIP_CONSOLE_URL", "http://localhost:8091")
TIMEOUT = 30.0

#: Every prediction object must carry these, suppressed or not (house rule 4).
MANDATORY_PREDICTION_KEYS = (
    "model_version",
    "generated_at",
    "confidence_score",
    "evidence_count",
    "evidence_tier",
    "suppression_reason",
)

#: Keys that can only ever hold a model output. None of them may appear inside
#: a block tagged ``kind="observed"`` (house rule 1).
PREDICTED_ONLY_KEYS = frozenset({
    "p10", "p50", "p90", "quantiles", "expected_value", "win_probability",
    "confidence_score", "evidence_tier", "recommended_bid", "rank_distribution",
    "undercut_probabilities", "price_to_beat", "participation_probability",
})

#: Money keys whose value must be the {amount, currency, vat_semantics} envelope.
MONEY_KEYS = frozenset({
    "p10", "p50", "p90", "expected_value", "price", "booklet_price",
    "median_award_value", "award_value", "estimated_cost", "proposed_bid",
    "risk_reserve", "recommended_bid", "expected_contribution", "price_to_beat",
    "actual_award_value", "hard_cost_floor", "max_bid", "contribution",
    "user_bid",
})

CURRENCY = "SAR"
VALID_TIERS = ("A", "B", "C", "D")
LABELS_AR = {"observed": "مُلاحظ", "predicted": "متوقع", "user_input": "إدخالك"}

#: Wording that would assert knowledge of a competitor's future bid (rule 2).
#: Affirmative constructions only: the shipped disclaimer legitimately contains
#: "سيقدمونه" inside the negation "وليست معرفة بما سيقدمونه فعلاً", and a bare
#: substring check would fail on the very sentence that enforces the rule.
FORBIDDEN_CERTAINTY_AR = (
    "سيقدم المنافس", "سوف يقدم المنافس", "سيعرض المنافس",
    "سيكون سعر المنافس", "المنافس سيقدم",
)


# --------------------------------------------------------------------------
# session fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def client() -> Any:
    try:
        with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as c:
            c.get("/api/stats").raise_for_status()
            yield c
    except (httpx.HTTPError, OSError) as exc:  # pragma: no cover - env dependent
        pytest.skip(f"console API not reachable at {BASE_URL}: {exc}")


def _tender_ids(client: Any, **params: Any) -> list[int]:
    r = client.get("/api/tenders", params=params)
    r.raise_for_status()
    return [int(i["id"]) for i in r.json()["items"]]


@pytest.fixture(scope="session")
def corpus(client: Any) -> dict[str, Any]:
    """Find a real thin-evidence tender and the richest one available.

    Deliberately discovered from the live corpus rather than hardcoded: tender
    ids churn on every re-ingest, and a battery pinned to ids silently stops
    testing anything the day the corpus is refreshed.
    """
    rich: dict[str, Any] | None = None
    thin: dict[str, Any] | None = None
    best_conf = -1

    open_ids = _tender_ids(client, open_only=True, limit=60)
    closed_ids = _tender_ids(client, awarded=True, limit=60)

    for tid in open_ids[:14]:
        payload = client.get(f"/api/tenders/{tid}/market-intelligence").json()
        pred = payload["prediction"]
        if pred["is_suppressed"]:
            thin = thin or {"tender_id": tid, "payload": payload}
            continue
        conf = pred["confidence_score"] or 0
        if conf > best_conf:
            best_conf, rich = conf, {"tender_id": tid, "payload": payload}

    if thin is None:
        for tid in closed_ids[:14]:
            payload = client.get(f"/api/tenders/{tid}/market-intelligence").json()
            if payload["prediction"]["is_suppressed"]:
                thin = {"tender_id": tid, "payload": payload}
                break

    if rich is None:
        pytest.skip("no tender in the live corpus yields an unsuppressed market range")
    return {"rich": rich, "thin": thin}


@pytest.fixture(scope="session")
def scenario(client: Any, corpus: dict[str, Any]) -> dict[str, Any]:
    tid = corpus["rich"]["tender_id"]
    mid = corpus["rich"]["payload"]["prediction"]["quantiles"]["p50"]["amount"]
    r = client.post(
        f"/api/tenders/{tid}/scenarios",
        json={
            "estimated_cost": round(mid * 0.6, 2),
            "min_margin_pct": 8.0,
            "target_win_pct": 60.0,
            "proposed_bid": round(mid * 0.95, 2),
            "name": "battery-api",
        },
    )
    r.raise_for_status()
    return r.json()["scenario"]


@pytest.fixture(scope="session")
def curve(client: Any, scenario: dict[str, Any]) -> dict[str, Any]:
    r = client.get(f"/api/scenarios/{scenario['id']}/curve", params={"refresh": True})
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# generic walkers
# --------------------------------------------------------------------------

def walk(node: Any, path: str = "$") -> Any:
    """Yield every (path, dict) pair in a JSON document."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk(value, f"{path}[{i}]")


def prediction_objects(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every prediction envelope in a response, found structurally."""
    return [
        (path, node)
        for path, node in walk(payload)
        if node.get("kind") == "predicted" and "is_suppressed" in node
    ]


def observed_blocks(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(p, n) for p, n in walk(payload) if n.get("kind") == "observed"]


def is_amount(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) >= {"amount", "currency", "vat_semantics"}
        and isinstance(value["amount"], (int, float))
    )


# --------------------------------------------------------------------------
# 1. envelope contract
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def prediction_payloads(client: Any, corpus: dict[str, Any], curve: dict[str, Any]):
    tid = corpus["rich"]["tender_id"]
    comp = client.get(f"/api/tenders/{tid}/competitors", params={"limit": 8}).json()
    out = {
        "market-intelligence": corpus["rich"]["payload"],
        "competitors": comp,
        "scenario-curve": curve,
    }
    if corpus["thin"] is not None:
        out["market-intelligence(thin)"] = corpus["thin"]["payload"]
    pid = corpus["rich"]["payload"]["prediction"]["prediction_id"]
    out["explanation"] = client.get(f"/api/predictions/{pid}/explanation").json()
    return out


def test_every_prediction_carries_the_mandatory_envelope(prediction_payloads):
    """House rule 4, checked structurally over every prediction in every response."""
    seen = 0
    for name, payload in prediction_payloads.items():
        if name == "explanation":
            missing = [k for k in MANDATORY_PREDICTION_KEYS if k not in payload]
            assert not missing, f"explanation missing {missing}"
            seen += 1
            continue
        objects = prediction_objects(payload)
        assert objects, f"{name} carries no prediction envelope"
        for path, pred in objects:
            missing = [k for k in MANDATORY_PREDICTION_KEYS if k not in pred]
            assert not missing, f"{name} {path} missing {missing}"
            assert pred["model_version"], f"{name} {path} blank model_version"
            assert pred["evidence_tier"] in VALID_TIERS, f"{name} {path} bad tier"
            assert isinstance(pred["evidence_count"], int)
            seen += 1
    assert seen >= 4, f"only {seen} prediction envelopes exercised"


def test_suppressed_predictions_carry_no_numbers(prediction_payloads):
    """Rule 3: suppression must be silence, not a quieter number."""
    checked = 0
    for name, payload in prediction_payloads.items():
        if name == "explanation":
            continue
        for path, pred in prediction_objects(payload):
            if not pred["is_suppressed"]:
                assert pred["suppression_reason"] is None, f"{name} {path}"
                continue
            checked += 1
            assert pred["suppression_reason"], f"{name} {path} suppressed with no reason"
            for key in ("quantiles", "expected_value", "win_probability"):
                assert pred[key] is None, f"{name} {path} suppressed but {key} set"
    assert checked, "no suppressed prediction observed; suppression path untested"


def test_response_envelopes_are_versioned(prediction_payloads):
    for name in ("market-intelligence", "competitors", "scenario-curve"):
        payload = prediction_payloads[name]
        assert payload.get("model_version"), f"{name} has no top-level model_version"
        assert payload.get("generated_at"), f"{name} has no top-level generated_at"


# --------------------------------------------------------------------------
# 2. observed vs predicted separation (house rule 1)
# --------------------------------------------------------------------------

def test_no_predicted_value_inside_an_observed_block(prediction_payloads):
    blocks = 0
    for name, payload in prediction_payloads.items():
        for path, block in observed_blocks(payload):
            blocks += 1
            assert block.get("label_ar") in (None, LABELS_AR["observed"]), (
                f"{name} {path} observed block mislabelled {block.get('label_ar')!r}")
            for subpath, node in walk(block, path):
                if node is block:
                    leaked = PREDICTED_ONLY_KEYS & set(node)
                    assert not leaked, f"{name} {subpath} observed block exposes {leaked}"
                    continue
                assert node.get("kind") != "predicted", (
                    f"{name} {subpath} is a predicted object nested in an observed block")
                leaked = PREDICTED_ONLY_KEYS & set(node) - MONEY_KEYS
                assert not leaked, f"{name} {subpath} inside observed block exposes {leaked}"
    assert blocks >= 2, f"only {blocks} observed blocks seen"


def test_predicted_and_user_input_blocks_are_labelled_in_arabic(prediction_payloads, curve):
    for payload in prediction_payloads.values():
        for path, pred in prediction_objects(payload):
            assert pred["label_ar"] == LABELS_AR["predicted"], path
    inputs = curve["scenario"]["inputs"]
    assert inputs["kind"] == "user_input"
    assert inputs["label_ar"] == LABELS_AR["user_input"]


def test_no_endpoint_claims_certainty_about_a_competitor(client, corpus):
    tid = corpus["rich"]["tender_id"]
    body = client.get(f"/api/tenders/{tid}/competitors", params={"limit": 8}).text
    for phrase in FORBIDDEN_CERTAINTY_AR:
        assert phrase not in body, f"competitors response asserts certainty: {phrase!r}"
    payload = client.get(f"/api/tenders/{tid}/competitors", params={"limit": 8}).json()
    assert payload["disclaimer"], "competitor response ships no disclaimer"
    assert "وليست معرفة" in payload["disclaimer"]


# --------------------------------------------------------------------------
# 3. evidence tier behaviour on REAL tenders
# --------------------------------------------------------------------------

def test_tier_contract_holds_for_every_real_competitor(client, corpus):
    """Tier C/D must have competitor prices suppressed; A/B may carry them.

    This is the one assertion that would catch the engine quietly inventing a
    competitor quantile out of one or two observations.
    """
    tid = corpus["rich"]["tender_id"]
    payload = client.get(f"/api/tenders/{tid}/competitors", params={"limit": 15}).json()
    if not payload["candidates"]:
        pytest.skip("no candidate bidders on the richest tender in the corpus")
    tiers: dict[str, int] = {}
    for cand in payload["candidates"]:
        tier = cand["evidence"]["tier"]
        pred = cand["price_prediction"]
        tiers[tier] = tiers.get(tier, 0) + 1
        assert tier in VALID_TIERS
        if tier in ("C", "D"):
            assert pred["is_suppressed"], (
                f"vendor {cand['vendor_id']} at tier {tier} carries a price range")
            assert pred["suppression_reason"], "suppressed without a machine-readable reason"
            assert pred["quantiles"] is None
        else:
            assert pred["evidence_count"] > 0, (
                f"vendor {cand['vendor_id']} at tier {tier} predicts from zero evidence")
    assert tiers, "no tiers observed"


def test_thin_evidence_tender_is_suppressed_not_extrapolated(corpus):
    """The corpus is thin; the contract is that the system says so."""
    thin = corpus["thin"]
    if thin is None:
        pytest.skip("no suppressed tender found in the sampled corpus")
    pred = thin["payload"]["prediction"]
    assert pred["is_suppressed"]
    assert pred["suppression_reason"] in {
        "INSUFFICIENT_EVIDENCE", "STALE_DATA", "LOW_SIMILARITY",
        "NO_COMPARABLE_TENDERS", "TENANT_DATA_UNAVAILABLE",
        "MODEL_UNAVAILABLE", "CALIBRATION_FAILED",
    }
    assert pred["evidence_tier"] == "D"
    assert thin["payload"]["evidence"]["comparable_count"] >= 0


def test_market_evidence_and_prediction_agree_about_suppression(prediction_payloads):
    payload = prediction_payloads["market-intelligence"]
    ev, pred = payload["evidence"], payload["prediction"]
    assert (ev["suppression"] is not None) == (pred["suppression_reason"] is not None), (
        "evidence block and prediction disagree about whether anything is claimable")
    if not pred["is_suppressed"]:
        assert ev["comparable_count"] > 0
        assert pred["evidence_count"] > 0
        q = pred["quantiles"]
        assert q["p10"]["amount"] <= q["p50"]["amount"] <= q["p90"]["amount"]


def test_simulation_excludes_every_suppressed_candidate(curve):
    """A competitor with no evidence must not silently enter the Monte Carlo."""
    if curve.get("suppressed"):
        assert curve["suppression_reason"], "suppressed curve with no reason"
        assert curve["curve"] == []
        return
    simulated = {d["vendor_id"] for d in curve["simulated_competitors"]}
    excluded = {e["vendor_id"] for e in curve["excluded_candidates"]}
    assert not (simulated & excluded), "a candidate is both simulated and excluded"
    for entry in curve["excluded_candidates"]:
        assert entry["reason"], f"vendor {entry['vendor_id']} excluded with no reason"


# --------------------------------------------------------------------------
# 4. confidence is not win probability
# --------------------------------------------------------------------------

def test_confidence_and_win_probability_are_distinct_fields(prediction_payloads):
    scored = 0
    for name, payload in prediction_payloads.items():
        if name == "explanation":
            continue
        for path, pred in prediction_objects(payload):
            assert "confidence_score" in pred and "win_probability" in pred, path
            assert pred.get("confidence_is_not_win_probability") is True, path
            conf = pred["confidence_score"]
            if conf is None:
                continue
            scored += 1
            assert 0 <= conf <= 100, f"{name} {path} confidence {conf} out of range"
            win = pred["win_probability"]
            if win is not None:
                assert 0.0 <= win <= 1.0
                assert abs(conf / 100.0 - win) > 1e-9 or conf in (0, 100), (
                    f"{name} {path} confidence and win probability are the same number")
    assert scored, "no scored prediction exercised"


def test_high_confidence_does_not_imply_high_win_probability(curve):
    """Confidence is a property of the evidence; win probability is a function of price.

    The proof on real data: one curve, one confidence score, many win
    probabilities. If confidence were a win probability in disguise it could
    not stay constant while the win probability moves across the grid.
    """
    market = curve["market_prediction"]
    if market["is_suppressed"]:
        pytest.skip("market suppressed for the scenario tender")
    conf = market["confidence_score"]
    assert conf is not None
    probs = [p["win_probability"] for p in curve["curve"]]
    assert probs, "curve carries no win probabilities"
    assert max(probs) - min(probs) > 0.10, (
        f"win probability barely moves across the price grid ({min(probs):.3f}.."
        f"{max(probs):.3f}); it is not tracking price")
    assert not any(abs(conf / 100.0 - p) < 1e-9 for p in probs) or len(set(probs)) > 1
    # Non-increasing in price, within Monte Carlo noise: a curve that rises with
    # price would mean the simulation is inverted.
    tol = max(3.0 * max(p["standard_error"] for p in curve["curve"]), 0.02)
    prices = [p["price"]["amount"] for p in curve["curve"]]
    assert prices == sorted(prices), "curve is not ordered by price"
    for (p_lo, w_lo), (p_hi, w_hi) in zip(
            list(zip(prices, probs))[:-1], list(zip(prices, probs))[1:], strict=True):
        assert w_hi <= w_lo + tol, (
            f"win probability rises with price: {p_lo:.0f}->{w_lo:.3f} "
            f"vs {p_hi:.0f}->{w_hi:.3f}")


def test_explanation_states_confidence_is_not_win_probability(prediction_payloads):
    exp = prediction_payloads["explanation"]
    assert exp.get("confidence_is_not_win_probability") is True
    assert "win_probability" not in exp


# --------------------------------------------------------------------------
# 5. amount formatting
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    ["market-intelligence", "competitors", "scenario-curve", "market-intelligence(thin)"],
)
def test_every_money_value_carries_currency_and_vat_semantics(prediction_payloads, name):
    if name not in prediction_payloads:
        pytest.skip(f"{name} not available in this corpus")
    payload = prediction_payloads[name]
    offenders = []
    for path, node in walk(payload):
        for key, value in node.items():
            if key not in MONEY_KEYS or value is None:
                continue
            if key == "price_to_beat" and not isinstance(value, (dict, list)):
                offenders.append((f"{path}.{key}", value))
                continue
            if isinstance(value, list):
                for i, item in enumerate(value):
                    if item is not None and not is_amount(item):
                        offenders.append((f"{path}.{key}[{i}]", item))
                continue
            if not is_amount(value):
                offenders.append((f"{path}.{key}", value))
                continue
            assert value["currency"] == CURRENCY, f"{path}.{key} currency {value['currency']}"
            assert value["vat_semantics"], f"{path}.{key} has no vat_semantics"
    assert not offenders, f"{name} ships bare money values: {offenders[:8]}"


def test_vat_semantics_is_explicit_and_honest(prediction_payloads):
    """`unknown` is a legitimate answer here — a silent `exclusive` would not be."""
    values = {
        node[key]["vat_semantics"]
        for _p, node in walk(prediction_payloads["market-intelligence"])
        for key in node
        if key in MONEY_KEYS and is_amount(node.get(key))
    }
    assert values, "no money value found to check"
    assert values <= {"unknown", "inclusive", "exclusive"}, values
    assert len(values) == 1, f"one response mixes VAT semantics: {values}"


# --------------------------------------------------------------------------
# 6. backwards compatibility of pre-existing endpoints
# --------------------------------------------------------------------------

PREEXISTING_GET_ENDPOINTS = [
    ("/api/stats", {"tenders", "offers", "awards"}),
    ("/api/dashboard", set()),
    ("/api/freshness/trend", set()),
    ("/api/filters", set()),
    ("/api/pursuits", set()),
    ("/api/lanes", set()),
    ("/api/ops/summary", set()),
    ("/api/settings", set()),
    ("/api/market/price-position", set()),
    ("/api/calibration", set()),
    ("/api/pricing/accuracy", set()),
    ("/api/outcomes/summary", set()),
    ("/api/vendors", set()),
    ("/api/agencies", set()),
    ("/api/profiles", set()),
    ("/api/notifications", set()),
    ("/api/tenders", {"total", "items"}),
    ("/api/search?q=%D8%B5%D9%8A%D8%A7%D9%86%D8%A9", {"total", "items"}),
]


@pytest.mark.parametrize("path,required", PREEXISTING_GET_ENDPOINTS,
                         ids=[p for p, _ in PREEXISTING_GET_ENDPOINTS])
def test_preexisting_endpoint_still_returns_its_shape(client, path, required):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert isinstance(body, (dict, list)), path
    if required:
        assert required <= set(body), f"{path} lost keys {required - set(body)}"


def test_preexisting_per_tender_endpoints_still_work(client, corpus):
    tid = corpus["rich"]["tender_id"]
    for path in (
        f"/api/tenders/{tid}",
        f"/api/tenders/{tid}/price-curve",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
        assert isinstance(r.json(), dict), path


def test_legacy_competitor_prices_is_retired(client, corpus):
    """The ungated per-vendor price-to-beat panel is deliberately gone.

    It published an actionable price from as little as one observed offer, with
    no evidence tier, no confidence and no suppression — on the same tenders
    where the gated engine correctly returns no competitor at all. Retiring it
    is a BLOCKING readiness fix, so this asserts the retirement rather than the
    old 200 contract, and names the supported replacement.
    """
    tid = corpus["rich"]["tender_id"]
    r = client.get(f"/api/tenders/{tid}/competitor-prices")
    assert r.status_code == 410, f"expected 410 Gone, got {r.status_code}"
    detail = r.json()["detail"]
    assert detail["error"] == "endpoint_retired"
    assert detail["use_instead"] == f"/api/tenders/{tid}/competitors"
    assert detail["reason_ar"].strip()
    # the replacement must actually answer
    replacement = client.get(detail["use_instead"])
    assert replacement.status_code == 200, replacement.text[:200]


def test_csv_export_still_streams_csv(client):
    r = client.get("/api/tenders.csv")
    assert r.status_code == 200
    assert "csv" in r.headers.get("content-type", "")
    assert r.text.splitlines()[0].count(",") >= 3


def test_unknown_tender_is_404_not_500(client):
    for suffix in ("/market-intelligence", "/competitors"):
        r = client.get(f"/api/tenders/999999999{suffix}")
        assert r.status_code == 404, f"{suffix} -> {r.status_code}"


def test_lineage_rejects_an_unknown_fact_table(client, corpus):
    r = client.get("/api/internal/lineage/passwords/1")
    assert r.status_code == 422
    ok = client.get(f"/api/internal/lineage/tenders/{corpus['rich']['tender_id']}")
    assert ok.status_code == 200
    assert "traceable" in ok.json()


# --------------------------------------------------------------------------
# 7. determinism and tenant isolation on the wire
# --------------------------------------------------------------------------

def test_scenario_curve_is_reproducible_from_seed(client, scenario):
    a = client.get(f"/api/scenarios/{scenario['id']}/curve", params={"refresh": True}).json()
    b = client.get(f"/api/scenarios/{scenario['id']}/curve", params={"refresh": True}).json()
    assert a["seed"] == b["seed"] == scenario["seed"]
    assert a["curve"] == b["curve"], "same seed, different curve"
    assert a.get("simulation") == b.get("simulation"), "same seed, different simulation"


def test_shared_endpoints_never_echo_tenant_private_inputs(client, corpus, scenario):
    """Rule 6: cost, margin and bid live only on the tenant's own routes."""
    tid = corpus["rich"]["tender_id"]
    for path in (f"/api/tenders/{tid}/market-intelligence",
                 f"/api/tenders/{tid}/competitors"):
        body = client.get(path).text
        for private in ("estimated_cost", "min_margin_pct", "proposed_bid",
                        "risk_reserve", "target_win_pct"):
            assert private not in body, f"{path} leaks {private}"
        assert str(scenario["inputs"]["estimated_cost"]["amount"]) not in body


# --------------------------------------------------------------------------
# 8. latency — measured, then reported
# --------------------------------------------------------------------------

def _latencies(fn, n: int) -> tuple[float, float, list[float]]:
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return p50, p95, samples


@pytest.mark.parametrize("n", [20])
def test_market_intelligence_latency(client, corpus, record_property, n):
    tid = corpus["rich"]["tender_id"]
    p50, p95, _ = _latencies(
        lambda: client.get(f"/api/tenders/{tid}/market-intelligence").raise_for_status(), n)
    record_property("market_intelligence_p50_ms", round(p50, 1))
    record_property("market_intelligence_p95_ms", round(p95, 1))
    print(f"\nmarket-intelligence over {n} calls: p50={p50:.1f}ms p95={p95:.1f}ms "
          f"(NFR: uncached p95 < 8000ms)")
    assert p95 < 8000.0, f"uncached p95 {p95:.1f}ms exceeds the 8s NFR"


@pytest.mark.parametrize("n", [20])
def test_scenario_curve_cached_latency(client, scenario, record_property, n):
    sid = scenario["id"]
    client.get(f"/api/scenarios/{sid}/curve", params={"refresh": True})
    p50, p95, _ = _latencies(
        lambda: client.get(f"/api/scenarios/{sid}/curve").raise_for_status(), n)
    record_property("scenario_curve_cached_p50_ms", round(p50, 1))
    record_property("scenario_curve_cached_p95_ms", round(p95, 1))
    print(f"scenario curve (cached) over {n} calls: p50={p50:.1f}ms p95={p95:.1f}ms "
          f"(NFR: curve < 500ms)")
    assert p95 < 500.0, f"cached curve p95 {p95:.1f}ms exceeds the 500ms NFR"


def test_scenario_curve_uncached_latency(client, scenario, record_property):
    sid = scenario["id"]
    p50, p95, _ = _latencies(
        lambda: client.get(f"/api/scenarios/{sid}/curve",
                           params={"refresh": True}).raise_for_status(), 20)
    record_property("scenario_curve_uncached_p50_ms", round(p50, 1))
    record_property("scenario_curve_uncached_p95_ms", round(p95, 1))
    print(f"scenario curve (recomputed) over 20 calls: p50={p50:.1f}ms p95={p95:.1f}ms "
          f"(NFR: uncached p95 < 8000ms)")
    assert p95 < 8000.0, f"uncached curve p95 {p95:.1f}ms exceeds the 8s NFR"


def test_cache_hit_is_marked_and_identical(client, scenario):
    sid = scenario["id"]
    fresh = client.get(f"/api/scenarios/{sid}/curve", params={"refresh": True}).json()
    hit = client.get(f"/api/scenarios/{sid}/curve").json()
    assert fresh["cache"] == "miss" and hit["cache"] == "hit"
    assert fresh["curve"] == hit["curve"], "cache hit disagrees with a recomputation"


# --------------------------------------------------------------------------
# 9. feedback loop contract
# --------------------------------------------------------------------------

def test_feedback_scores_against_the_stored_interval(client, corpus):
    pred = corpus["rich"]["payload"]["prediction"]
    pid = pred["prediction_id"]
    inside = pred["quantiles"]["p50"]["amount"]
    r = client.post(f"/api/predictions/{pid}/feedback",
                    json={"actual_award_value": inside})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scoring"]["interval_hit"] is True
    assert body["scoring"]["abs_pct_error"] == pytest.approx(0.0, abs=1e-4)
    assert body["observed"]["kind"] == "observed"
    assert is_amount(body["observed"]["actual_award_value"])

    outside = pred["quantiles"]["p90"]["amount"] * 10
    body2 = client.post(f"/api/predictions/{pid}/feedback",
                        json={"actual_award_value": outside}).json()
    assert body2["scoring"]["interval_hit"] is False


def test_feedback_on_a_suppressed_prediction_is_not_scored(client, corpus):
    thin = corpus["thin"]
    if thin is None:
        pytest.skip("no suppressed prediction available")
    pid = thin["payload"]["prediction"]["prediction_id"]
    body = client.post(f"/api/predictions/{pid}/feedback",
                       json={"actual_award_value": 12345.0}).json()
    assert body["scoring"]["interval_hit"] is None
    assert body["scoring"]["abs_pct_error"] is None
    assert "suppressed" in (body["scoring"]["note"] or "")


def test_a_second_tenant_cannot_read_the_first_tenants_scenario(client, scenario):
    """Rule 6 on the wire: scenario routes are keyed by tenant, not just by id."""
    other = client.get(f"/api/scenarios/{scenario['id']}/curve",
                       headers={"X-Thaqip-Tenant": "redteam"})
    if other.status_code == 404 and "unknown tenant" in other.text:
        pytest.skip("no second tenant provisioned in this database")
    assert other.status_code == 404, (
        f"another tenant read scenario {scenario['id']} -> {other.status_code}")


def test_explanation_money_values_are_enveloped(prediction_payloads):
    exp = prediction_payloads["explanation"]
    if exp.get("is_suppressed"):
        assert exp.get("quantiles") is None
        return
    for key in ("p10", "p50", "p90"):
        assert is_amount(exp["quantiles"][key]), f"explanation {key} is a bare number"
        assert exp["quantiles"][key]["currency"] == CURRENCY


def test_explanation_separates_factor_kinds(prediction_payloads):
    """House rule 1 reaches the factor list too: each factor declares its kind."""
    exp = prediction_payloads["explanation"]
    factors = exp.get("factors") or exp.get("explanation_factors") or []
    if not factors:
        pytest.skip("stored prediction carries no explanation factors")
    kinds = {f["kind"] for f in factors}
    assert kinds <= {"observed", "derived", "predicted"}, kinds
    for f in factors:
        assert f["direction"] in ("increases", "decreases", "neutral"), f
        assert f["name"] and isinstance(f["weight"], (int, float))
