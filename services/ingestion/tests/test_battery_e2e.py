"""End-to-end UI, RTL and accessibility battery for the Price-to-Win console.

What this battery is for
------------------------
Every other battery in this repo asserts against Python objects or JSON. None of
them can see the surface a bidder actually reads. A P2W prediction that is
correct in ``PricePrediction.to_dict()`` and invisible, mislabelled or silently
suppressed in the browser is still a wrong product, and the house rules (a
provenance badge on every number; suppression that explains itself in Arabic and
keeps the observed facts on screen; no fabricated recommendation) are rules about
the *rendered DOM*, not about the payload.

So this battery drives a real Chromium against the running console on
``localhost:8091`` with Playwright, and asserts on what is on screen.

How it is wired
---------------
``pytest-playwright`` is deliberately NOT a dependency. The repo already declares
the ``browser`` extra (``playwright>=1.62``) for the Etimad crawler, and the
sync API is enough here, so the battery uses ``playwright.sync_api`` directly
inside plain sync tests. Run it with::

    cd services/ingestion && uv run --extra browser pytest -q tests/test_battery_e2e.py

Without ``--extra browser`` (or without the console up) every test in this file
skips rather than failing: the rest of the suite must stay runnable offline.

Determinism
-----------
The only nondeterminism this battery tolerates is the corpus itself, so the
subject tenders are resolved at session start by *querying the live API* for one
tender whose market prediction is unsuppressed and one whose is suppressed,
rather than by hardcoding ids that a re-ingest would invalidate. If the corpus
cannot supply one of those, the affected tests skip with the reason stated —
they never soften into a weaker assertion.

Corpus limitation, stated plainly
---------------------------------
``market_evidence()`` enters ``classify_tier()`` with ``effective_competitor_n=0``
by design, so a MARKET-scope prediction can never be classified above Tier C.
Measured on the live corpus: 307 awarded tenders, 28 Tier C, 279 Tier D, zero A
or B. The consequence for this battery is documented on
``test_competitor_layer_is_not_gated_on_the_market_tier``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _live_auth import auth_headers, credential

API = os.environ.get("THAQIP_CONSOLE_URL", "http://localhost:8091").rstrip("/")
SHOTS = Path(
    os.environ.get(
        "THAQIP_E2E_SHOTS",
        "/tmp/claude-1000/-home-ais04/16788e65-71bd-470c-9f78-fa9e9b878237/scratchpad/e2e-shots",
    )
)

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

PROV_SELECTOR = ".prov-observed, .prov-predicted, .prov-user"

# Arabic provenance labels — house rule 1. These strings are load-bearing:
# they are what tells a reader an observed fact from a model output.
AR_OBSERVED = "مُلاحظ"
AR_PREDICTED = "متوقع"
AR_USER = "إدخالك"


# --------------------------------------------------------------------------
# environment probes
# --------------------------------------------------------------------------
def _http_json(path: str, payload: dict | None = None, timeout: float = 60.0) -> Any:
    url = API + path
    data = None
    headers = auth_headers()
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _console_up() -> bool:
    try:
        urllib.request.urlopen(urllib.request.Request(
            API + "/api/settings", headers=auth_headers()), timeout=5).read()
    except urllib.error.HTTPError as exc:
        pytest.fail(f"console refused the battery's credential: HTTP {exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
    return True


try:  # pragma: no cover - import guard, exercised by the skip path
    from playwright.sync_api import sync_playwright

    _HAVE_PW = True
except Exception:  # noqa: BLE001  # pragma: no cover
    _HAVE_PW = False
    sync_playwright = None  # type: ignore[assignment]


def _browser_available() -> bool:
    if not _HAVE_PW:
        return False
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            b.close()
    except Exception:  # noqa: BLE001 - any launch failure means "no browser here"
        return False
    return True


pytestmark = [
    pytest.mark.live_api,
    pytest.mark.skipif(not _HAVE_PW, reason="playwright not installed (use --extra browser)"),
    pytest.mark.skipif(not _console_up(), reason=f"console API not reachable at {API}"),
]


# --------------------------------------------------------------------------
# subjects, resolved from the live corpus
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Subject:
    tender_id: int
    tier: str | None
    allowed_level: str | None
    suppressed: bool
    reason: str | None
    comparable_count: int
    similar_used: int
    quantiles: dict[str, float] = field(default_factory=dict)


def _probe(tender_id: int) -> Subject | None:
    try:
        d = _http_json(f"/api/tenders/{tender_id}/market-intelligence")
    except Exception:  # noqa: BLE001 - an unprobeable tender is simply not a subject
        return None
    p = d["prediction"]
    q = p.get("quantiles") or {}
    return Subject(
        tender_id=tender_id,
        tier=p.get("evidence_tier"),
        allowed_level=d.get("allowed_level"),
        suppressed=bool(p["is_suppressed"]),
        reason=p.get("suppression_reason"),
        comparable_count=(d.get("evidence") or {}).get("comparable_count") or 0,
        similar_used=len(((d.get("similar_tenders") or {}).get("used")) or []),
        quantiles={k: (v or {}).get("amount") for k, v in q.items()} if q else {},
    )


@pytest.fixture(scope="session")
def subjects() -> dict[str, Subject]:
    """One tender with a live market range and one with a suppressed one.

    Resolved by walking the tender list rather than hardcoding, so a re-ingest
    does not silently turn this battery into a no-op against stale ids.
    """
    # Only awarded tenders can carry an unsuppressed market range, so start
    # there; the endpoint caps `limit` at 200, hence the explicit paging.
    ids: list[int] = []
    for offset in (0, 200):
        page = _http_json(f"/api/tenders?awarded=true&limit=200&offset={offset}")
        ids.extend(t["id"] for t in page.get("items", []))
        if len(page.get("items", [])) < 200:
            break
    rich: Subject | None = None
    thin: Subject | None = None
    for tid in ids:
        s = _probe(tid)
        if s is None:
            continue
        # "Rich" must be able to draw the whole page, including the bid curve,
        # which needs competitor-level prices: capability level L3 or above.
        # Picking the first unsuppressed market (often tier C) made the curve
        # tests depend on which awards the harvester found most recently.
        if not s.suppressed and s.allowed_level in ("L3", "L4") and rich is None:
            rich = s
        # A thin subject is only interesting if it still has observed
        # comparables to show: that is exactly the state the house rules say
        # must stay on screen underneath the suppression notice.
        if s.suppressed and s.similar_used > 0 and thin is None:
            thin = s
        if rich and thin:
            break
    return {"rich": rich, "thin": thin}  # type: ignore[dict-item]


@pytest.fixture(scope="session")
def rich(subjects) -> Subject:
    if subjects["rich"] is None:
        pytest.skip("no tender in the live corpus yields an unsuppressed market range")
    return subjects["rich"]


@pytest.fixture(scope="session")
def thin(subjects) -> Subject:
    if subjects["thin"] is None:
        pytest.skip("no tender in the live corpus yields a suppressed market range")
    return subjects["thin"]


# --------------------------------------------------------------------------
# browser plumbing
# --------------------------------------------------------------------------
class ConsoleLog:
    """Collects console errors and uncaught page errors.

    Resource-load failures are kept apart from JS errors: a blocked Google Fonts
    stylesheet is a network fact about the sandbox, not a defect in the page, and
    conflating the two would let a real TypeError hide behind an offline run.
    """

    _NET = re.compile(
        r"failed to load resource|net::ERR_|ERR_NAME_NOT_RESOLVED|ERR_INTERNET_DISCONNECTED",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        self.js: list[str] = []
        self.network: list[str] = []

    def attach(self, page) -> None:
        def on_console(msg):
            if msg.type == "error":
                (self.network if self._NET.search(msg.text) else self.js).append(msg.text)

        page.on("console", on_console)
        page.on("pageerror", lambda e: self.js.append(f"pageerror: {e}"))


@pytest.fixture
def _pw() -> Iterator[Any]:
    """Function-scoped on purpose — this is a defect this battery caused and fixed.

    Playwright's *sync* API drives its asyncio loop by greenlet switching on the
    calling thread, so while a ``sync_playwright()`` context is open there is a
    RUNNING event loop in that thread. Holding it open at session scope left that
    loop running for every test collected after this file, and pytest-asyncio's
    ``asyncio.run()`` then died with "Runner.run() cannot be called from a running
    event loop" — 154 failures across the suite, none of them in this file, and
    invisible whenever this file was run on its own.

    Entering and exiting the context per test keeps the leak inside the test that
    creates it. Measured cost: a chromium launch/close is well under a second.
    """
    if not _browser_available():
        pytest.skip("chromium is not installed for playwright on this machine")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()


def _signed_in_context(browser, **kwargs):
    """A browser context holding a real session cookie, obtained through the
    same /api/auth/login call the login form makes."""
    user, password = credential("THAQIP_ADMIN_USER"), credential("THAQIP_ADMIN_PASSWORD")
    if not (user and password):
        pytest.skip("no admin credential (var/credentials.env) for the browser battery")
    ctx = browser.new_context(**kwargs)
    resp = ctx.request.post(API + "/api/auth/login",
                            data={"username": user, "password": password})
    assert resp.ok, f"login failed: {resp.status} {resp.text()[:200]}"
    return ctx


@pytest.fixture
def session(_pw) -> Iterator[tuple[Any, ConsoleLog]]:
    ctx = _signed_in_context(_pw, viewport=DESKTOP, locale="ar-SA")
    page = ctx.new_page()
    log = ConsoleLog()
    log.attach(page)
    page.goto(API + "/", wait_until="networkidle")
    yield page, log
    ctx.close()


def _shot(page, name: str) -> Path:
    SHOTS.mkdir(parents=True, exist_ok=True)
    p = SHOTS / f"{name}.png"
    page.screenshot(path=str(p), full_page=True)
    return p


def _open_p2w(page) -> None:
    """Reach the P2W view the way a user does, from whichever nav is on screen.

    The desktop rail is ``lg:flex`` and hidden below 1024px, so a mobile run has
    to go through the bottom bar. Clicking the hidden one instead of the visible
    one is the difference between testing the app and testing a selector.
    """
    desktop = page.locator('#nav .navbtn[data-view="p2w"]')
    mobile = page.locator('#navMobile .navbtn[data-view="p2w"]')
    (desktop if desktop.is_visible() else mobile).click()
    page.wait_for_selector("#view-p2w:not(.hidden)")


def _run_for(page, tender_id: int) -> None:
    """Select a tender and run the three layers, through the page's own code."""
    _open_p2w(page)
    # Fire-and-wait: returning the promise to Playwright makes the handle a
    # GC candidate, so kick the page's own code off and wait on the DOM.
    page.evaluate(f"() => {{ p2wSelect({tender_id}); }}")
    page.wait_for_selector("#p2wRun", timeout=30_000)
    page.click("#p2wRun")
    page.wait_for_selector("#p2wBody:not(.hidden)", timeout=30_000)
    page.wait_for_selector("#p2wLayerBid .p2w-layer-bid", timeout=30_000)


def _text(page, selector: str) -> str:
    return page.inner_text(selector)


# --------------------------------------------------------------------------
# 1. clean load
# --------------------------------------------------------------------------
def test_p2w_view_loads_with_zero_js_console_errors(session, rich):
    page, log = session
    _run_for(page, rich.tender_id)
    shot = _shot(page, "01-p2w-loaded")
    assert shot.exists()
    assert log.js == [], f"JS console errors on the Price-to-Win view: {log.js}"


def test_p2w_thin_evidence_load_is_also_error_free(session, thin):
    page, log = session
    _run_for(page, thin.tender_id)
    _shot(page, "02-p2w-suppressed")
    assert log.js == [], f"JS console errors rendering the suppressed state: {log.js}"


# --------------------------------------------------------------------------
# 2. the three layers / the suppressed state
# --------------------------------------------------------------------------
def test_high_evidence_tender_shows_all_three_layers(session, rich):
    page, _ = session
    _run_for(page, rich.tender_id)

    market = _text(page, "#p2wLayerMarket")
    assert "السوق" in market
    # The quantile trio must actually carry the API's numbers.
    for key in ("p10", "p50", "p90"):
        amount = rich.quantiles.get(key)
        assert amount is not None
        assert f"{round(amount):,}" in market, f"{key} ({amount}) missing from the market layer"
    assert "P10" in market and "P50" in market and "P90" in market

    assert page.locator("#p2wLayerMarket svg").count() >= 1, "range bar SVG did not render"
    assert page.locator("#p2wLayerCompetitors .p2w-layer-comp").count() == 1
    assert page.locator("#p2wLayerBid .p2w-layer-bid").count() == 1
    for field_id in ("#p2wCost", "#p2wMargin", "#p2wTarget", "#p2wReserve"):
        assert page.locator(field_id).count() == 1, f"missing bid input {field_id}"


def test_thin_evidence_suppresses_the_range_but_keeps_observed_facts(session, thin):
    page, _ = session
    _run_for(page, thin.tender_id)
    market = _text(page, "#p2wLayerMarket")

    assert page.locator("#p2wLayerMarket .p2w-suppressed").count() >= 1, (
        "a suppressed market prediction must render the designed suppression panel"
    )
    # Arabic explanation, not a bare code.
    assert "لا نعرض نطاق سعر لهذه المنافسة" in market
    assert re.search(r"[؀-ۿ]{8,}", market), "suppression panel has no Arabic prose"
    # Machine-readable reason is still surfaced.
    assert thin.reason and thin.reason in market, (
        f"machine-readable suppression reason {thin.reason!r} is not shown"
    )
    # No fabricated range.
    assert "P10" not in market and "P90" not in market, (
        "a suppressed prediction must not render quantiles"
    )
    # Observed facts survive the suppression.
    assert AR_OBSERVED in market
    assert "حقائق مُلاحظة" in market
    assert "المنافسات المشابهة المستخدمة" in market, (
        "comparable tenders must stay on screen under a suppressed prediction"
    )
    used_rows = page.locator("#p2wLayerMarket details table tbody tr")
    assert used_rows.count() >= 1, "comparable-tender table rendered empty"


# --------------------------------------------------------------------------
# 3. provenance badges
# --------------------------------------------------------------------------
def test_every_headline_number_carries_a_provenance_badge(session, rich):
    page, _ = session
    _run_for(page, rich.tender_id)
    # A cost below the market p10 keeps the optimizer feasible, so the result
    # cells actually render and can be checked for their badges.
    page.fill("#p2wCost", str(max(1000, int((rich.quantiles.get("p10") or 50_000) * 0.6))))
    page.click("#p2wCalcBtn")
    page.wait_for_selector("#p2wBidResult .num", timeout=60_000)

    # (a) the market range card owns a "متوقع" badge
    range_card = page.locator("#p2wLayerMarket .bg-card", has_text="نطاق السوق المتوقع").first
    assert range_card.locator(".prov-predicted").count() >= 1, (
        "the predicted market range has no متوقع badge"
    )

    # (b) the observed-facts block owns a "مُلاحظ" badge
    facts_head = page.locator("#p2wLayerMarket div", has_text="حقائق مُلاحظة").last
    assert facts_head.locator(".prov-observed").count() >= 1

    # (c) every user input in layer 3 is labelled as the user's own
    labels = page.locator("#p2wLayerBid label")
    assert labels.count() >= 4
    for i in range(labels.count()):
        lab = labels.nth(i)
        assert lab.locator(".prov-user").count() >= 1, (
            f"bid input #{i} is not labelled {AR_USER}"
        )

    # (d) every rendered *quantity* in layer 3 carries a badge inside its own
    #     cell — not merely somewhere in the surrounding panel.
    #
    #     A quantity is a `.num` node whose whole text is a number, optionally
    #     with a percent sign or the ر.س unit. That deliberately excludes the
    #     reproducibility metadata rendered in the same mono face — the seed, the
    #     model version, the evaluation-rule token, the SCREAMING_SNAKE
    #     suppression reason, the optimizer's infeasibility sentence. Those are
    #     identifiers and prose, not amounts, and a provenance badge on them
    #     would be noise. Anything that reads as a number must be attributable.
    bad = page.evaluate(
        """() => {
          const QUANTITY = /^-?[\\d][\\d,\\.\\u066B\\u066C]*\\s*(%|ر\\.س)?$/u;
          const out = [];
          document.querySelectorAll('#p2wBidResult .num').forEach(n => {
            const txt = n.textContent.replace(/\\s+/g, ' ').trim();
            if (!QUANTITY.test(txt)) return;          // identifier / prose
            const cell = n.closest('div.bg-card, div.rounded-xl, div.rounded-2xl');
            if (!cell) { out.push('orphan:' + txt); return; }
            if (!cell.querySelector('.prov-observed,.prov-predicted,.prov-user'))
              out.push(txt + ' @ ' + cell.textContent.replace(/\\s+/g,' ').trim().slice(0, 50));
          });
          return out;
        }"""
    )
    assert bad == [], f"numbers rendered without a provenance badge: {bad}"

    # (e) the vocabulary itself. The prov-* palette classes are reused for the
    #     step chips and the confidence pill, so filter to the elements that are
    #     actually acting as a provenance label (pure Arabic text, no digits):
    #     those must use one of the three sanctioned words and nothing else, and
    #     all three must be present on a fully rendered surface.
    badges = page.locator(f"#p2wBody :is({PROV_SELECTOR})")
    assert badges.count() >= 8
    seen = {badges.nth(i).inner_text().strip() for i in range(badges.count())}
    labels = {t for t in seen if t and not any(ch.isdigit() for ch in t)}
    assert labels <= {AR_OBSERVED, AR_PREDICTED, AR_USER}, f"unexpected badge labels: {labels}"
    assert labels == {AR_OBSERVED, AR_PREDICTED, AR_USER}, (
        f"a full three-layer surface must show all three provenance kinds, saw {labels}"
    )
    _shot(page, "03-provenance")


# --------------------------------------------------------------------------
# 4. the bid layer
# --------------------------------------------------------------------------
def test_entering_a_cost_produces_a_curve_and_a_live_margin(session, rich):
    page, log = session
    _run_for(page, rich.tender_id)
    cost = max(1000.0, round((rich.quantiles.get("p10") or 50_000) * 0.6))
    page.fill("#p2wCost", str(int(cost)))
    page.click("#p2wCalcBtn")
    page.wait_for_selector("#p2wCurve svg", timeout=60_000)

    assert page.locator("#p2wSlider").count() == 1
    before = _text(page, "#p2wSliderOut")
    assert "الهامش عنده" in before

    slider = page.locator("#p2wSlider")
    lo = float(slider.get_attribute("min"))
    hi = float(slider.get_attribute("max"))
    page.evaluate(
        "([v]) => { const s = document.getElementById('p2wSlider');"
        " s.value = String(v); s.dispatchEvent(new Event('input')); }",
        [round(lo + (hi - lo) * 0.85)],
    )
    after = _text(page, "#p2wSliderOut")
    assert after != before, "moving the price slider did not update margin / win probability"

    # The margin the UI shows must be the margin the price implies.
    shown = page.evaluate(
        """() => {
             const s = document.getElementById('p2wSlider');
             return {price: Number(s.value),
                     cost: Number(document.getElementById('p2wCost').value),
                     text: document.getElementById('p2wSliderOut').innerText};
           }"""
    )
    expected = (shown["price"] - shown["cost"]) / shown["price"] * 100
    assert f"{expected:.1f}%" in shown["text"], (
        f"displayed margin does not match (price-cost)/price: expected {expected:.1f}%"
        f" in {shown['text']!r}"
    )
    _shot(page, "04-curve")
    assert log.js == [], f"JS console errors while driving the curve: {log.js}"


def test_cost_above_the_market_p90_yields_an_honest_infeasible_state(session, rich):
    """No recommendation may be invented when the constraints cannot be met."""
    page, _ = session
    _run_for(page, rich.tender_id)
    p90 = rich.quantiles.get("p90")
    assert p90, "subject has no p90 to exceed"
    page.fill("#p2wCost", str(int(p90 * 2.5)))
    page.fill("#p2wTarget", "60")
    page.click("#p2wCalcBtn")
    page.wait_for_selector("#p2wBidResult :is(.p2w-suppressed, #p2wCurve)", timeout=60_000)

    out = _text(page, "#p2wBidResult")
    api = page.evaluate("() => p2wState.curve")
    opt = api.get("optimizer") or {}

    if api.get("suppressed") or not api.get("curve"):
        assert "لا نرسم منحنى احتمالية فوز" in out
        return

    assert opt.get("is_feasible") is False, (
        "a cost far above the market p90 with a 60% win target should be infeasible;"
        f" optimizer said {opt}"
    )
    assert "لا يوجد سعر يحقّق كل قيودك" in out, "infeasible state is not stated to the user"
    assert opt.get("infeasible_reason"), "infeasible state carries no machine-readable reason"
    assert opt["infeasible_reason"] in out, "the infeasible reason is not shown on screen"
    # The killer: no fabricated recommendation anywhere on the surface.
    assert "السعر الموصى به" not in out, (
        "an infeasible scenario still rendered a recommended price"
    )
    _shot(page, "05-infeasible")


# --------------------------------------------------------------------------
# 5. the "لماذا؟" drill-down against the API
# --------------------------------------------------------------------------
def test_why_drilldown_numbers_match_the_explanation_endpoint(session, rich):
    page, log = session
    _run_for(page, rich.tender_id)

    why_btn = page.locator("#p2wLayerMarket button", has_text="لماذا؟").first
    assert why_btn.count() == 1
    pred_id = int(
        re.search(r"p2wWhy\((\d+)", why_btn.get_attribute("onclick") or "").group(1)  # type: ignore[union-attr]
    )
    why_btn.click()
    page.wait_for_selector("#p2wWhyPanel .bg-card", timeout=30_000)
    panel = _text(page, "#p2wWhyPanel")

    api = _http_json(f"/api/predictions/{pred_id}/explanation")

    assert str(api.get("evidence_count")) in panel
    assert str(api.get("data_freshness_score")) + "/100" in panel
    assert str(api.get("similarity_confidence")) + "/100" in panel
    assert api.get("model_version") in panel
    if api.get("feature_snapshot_id"):
        assert api["feature_snapshot_id"] in panel
    if api.get("headline"):
        assert api["headline"] in panel

    factors = api.get("factors") or []
    assert factors, "explanation endpoint returned no factors for an unsuppressed prediction"
    for f in factors:
        assert f["name"] in panel, f"factor {f['name']!r} missing from the لماذا؟ panel"
        assert f"{float(f['weight']):.2f}" in panel, (
            f"weight of {f['name']!r} ({f['weight']}) not rendered to 2dp in the panel"
        )
    _shot(page, "06-why")
    assert log.js == [], f"JS console errors opening the drill-down: {log.js}"


# --------------------------------------------------------------------------
# 6. RTL and layout
# --------------------------------------------------------------------------
@pytest.mark.parametrize("size,name", [(DESKTOP, "desktop-1440x900"), (MOBILE, "mobile-390x844")])
def test_rtl_layout_has_no_horizontal_overflow(_pw, rich, size, name):
    ctx = _signed_in_context(_pw, viewport=size, locale="ar-SA")
    page = ctx.new_page()
    log = ConsoleLog()
    log.attach(page)
    try:
        page.goto(API + "/", wait_until="networkidle")
        assert page.get_attribute("html", "dir") == "rtl"
        assert page.get_attribute("html", "lang") == "ar"
        _run_for(page, rich.tender_id)
        page.fill("#p2wCost", "50000")
        page.click("#p2wCalcBtn")
        page.wait_for_selector("#p2wCurve svg", timeout=60_000)
        # Layout width is only meaningful once webfonts have settled: IBM Plex
        # Arabic arrives from a CDN and pre-swap fallback metrics transiently
        # widen RTL rows. Without this the assertion below flakes under load.
        page.wait_for_function(
            "() => document.fonts && document.fonts.status === 'loaded'", timeout=30_000
        )
        page.wait_for_timeout(150)

        metrics = page.evaluate(
            """() => ({
                 scrollW: document.documentElement.scrollWidth,
                 clientW: document.documentElement.clientWidth,
                 bodyScrollW: document.body.scrollWidth,
               })"""
        )
        assert metrics["scrollW"] <= metrics["clientW"] + 1, (
            f"{name}: horizontal page overflow {metrics}"
        )
        assert metrics["bodyScrollW"] <= metrics["clientW"] + 1, f"{name}: body overflows {metrics}"

        # Any wide element must scroll inside its own container, never the page.
        offenders = page.evaluate(
            """() => {
                 const w = document.documentElement.clientWidth;
                 const out = [];
                 document.querySelectorAll('#view-p2w *').forEach(el => {
                   const r = el.getBoundingClientRect();
                   if (r.width === 0) return;
                   if (r.right > w + 1 || r.left < -1)
                     out.push((el.id || el.className || el.tagName).toString().slice(0, 70));
                 });
                 return out.slice(0, 8);
               }"""
        )
        assert offenders == [], f"{name}: elements escape the viewport horizontally: {offenders}"

        # Numerals are mono + tabular.
        num_font = page.evaluate(
            """() => {
                 const n = document.querySelector('#p2wLayerMarket .num');
                 if (!n) return null;
                 const cs = getComputedStyle(n);
                 return {family: cs.fontFamily, variant: cs.fontVariantNumeric};
               }"""
        )
        assert num_font, "no .num element rendered in the market layer"
        assert "IBM Plex Mono" in num_font["family"], num_font
        assert "tabular-nums" in num_font["variant"], num_font

        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"07-rtl-{name}.png"), full_page=True)
        assert log.js == [], f"{name}: JS console errors: {log.js}"
    finally:
        ctx.close()


def test_desktop_sidebar_sits_on_the_right(_pw):
    ctx = _signed_in_context(_pw, viewport=DESKTOP, locale="ar-SA")
    page = ctx.new_page()
    try:
        page.goto(API + "/", wait_until="networkidle")
        box = page.locator("aside").first.bounding_box()
        assert box, "sidebar has no box"
        assert box["x"] + box["width"] >= DESKTOP["width"] - 2, (
            f"RTL sidebar is not flush to the right edge: {box}"
        )
        assert box["x"] > DESKTOP["width"] / 2, f"RTL sidebar rendered on the left: {box}"
    finally:
        ctx.close()


def test_mobile_hides_the_desktop_sidebar_and_shows_the_bottom_nav(_pw):
    ctx = _signed_in_context(_pw, viewport=MOBILE, locale="ar-SA")
    page = ctx.new_page()
    try:
        page.goto(API + "/", wait_until="networkidle")
        assert not page.locator("aside").first.is_visible()
        assert page.locator('#navMobile .navbtn[data-view="p2w"]').is_visible()
    finally:
        ctx.close()


# --------------------------------------------------------------------------
# 7. keyboard
# --------------------------------------------------------------------------
def test_primary_workflow_is_reachable_by_tab(session, rich):
    page, _ = session
    _run_for(page, rich.tender_id)
    page.evaluate("() => document.body.focus()")
    page.keyboard.press("Home")

    wanted = {"p2wSearch", "p2wSearchBtn", "p2wRun", "p2wCost", "p2wCalcBtn"}
    seen: set[str] = set()
    for _ in range(160):
        page.keyboard.press("Tab")
        ident = page.evaluate(
            "() => { const a = document.activeElement;"
            " return a ? (a.id || '') : ''; }"
        )
        if ident:
            seen.add(ident)
        if wanted <= seen:
            break
    assert wanted <= seen, f"not reachable by Tab: {sorted(wanted - seen)} (saw {sorted(seen)})"


def test_focus_is_visible_on_the_primary_action(session, rich):
    page, _ = session
    _run_for(page, rich.tender_id)
    page.focus("#p2wRun")
    outline = page.evaluate(
        """() => { const cs = getComputedStyle(document.getElementById('p2wRun'));
                   return {w: cs.outlineWidth, s: cs.outlineStyle}; }"""
    )
    # focus-visible only paints on keyboard focus; force it the same way the
    # user would, then read the rule that is meant to fire.
    css = page.evaluate(
        """() => [...document.styleSheets].flatMap(s => { try { return [...s.cssRules]; }
             catch(e) { return []; } }).map(r => r.cssText)
             .filter(t => t.includes(':focus-visible')).join(' ')"""
    )
    assert ":focus-visible" in css and "outline" in css, (
        f"no focus-visible outline rule found (computed outline was {outline})"
    )


def test_drawer_closes_on_escape(session, rich):
    page, log = session
    _open_p2w(page)
    page.evaluate(f"() => {{ openDetail({rich.tender_id}); }}")
    page.wait_for_function(
        "() => document.getElementById('drawer').getAttribute('aria-hidden') === 'false'",
        timeout=30_000,
    )
    _shot(page, "08-drawer-open")
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => document.getElementById('drawer').getAttribute('aria-hidden') === 'true'",
        timeout=10_000,
    )
    assert "-translate-x-full" in (page.get_attribute("#drawer", "class") or "")
    assert page.locator("#overlay").is_hidden()
    assert log.js == [], f"JS console errors driving the drawer: {log.js}"


def test_why_panel_closes_on_escape(session, rich):
    """The drill-down is a dismissible overlay-like surface; Escape must close it.

    Regression guard for the defect fixed in this battery: the panel previously
    had only a mouse-driven ✕ and swallowed Escape, so a keyboard user could
    open it and had no way back.
    """
    page, _ = session
    _run_for(page, rich.tender_id)
    page.locator("#p2wLayerMarket button", has_text="لماذا؟").first.click()
    page.wait_for_selector("#p2wWhyPanel .bg-card", timeout=30_000)
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => document.getElementById('p2wWhyPanel').innerHTML.trim() === ''",
        timeout=10_000,
    )


# --------------------------------------------------------------------------
# 8. the competitor layer gate
# --------------------------------------------------------------------------
def test_competitor_layer_is_not_gated_on_the_market_tier(session, rich):
    """Layer 2 must reflect the competitor predictions the API actually returned.

    Why this is a real test and not a tautology: a MARKET-scope prediction enters
    ``classify_tier`` with ``effective_competitor_n=0``, so it can never be
    classified above Tier C. Gating the competitor layer on the *market* tier
    therefore suppresses layer 2 for every tender that will ever exist, including
    the ones where ``/api/tenders/{id}/competitors`` returned Tier-B candidates
    with real quantiles. This asserts the UI honours the per-candidate
    suppression decision instead.
    """
    page, _ = session
    _run_for(page, rich.tender_id)
    comp = page.evaluate("() => p2wState.comp")
    if not comp or comp.get("error") or not comp.get("candidates"):
        pytest.skip("the API returned no competitor candidates for this subject")

    priced = [
        c
        for c in comp["candidates"]
        if not (c.get("price_prediction") or {}).get("is_suppressed", True)
    ]
    if not priced:
        pytest.skip("every candidate is suppressed at source; nothing for the UI to show")

    layer = _text(page, "#p2wLayerCompetitors")
    name = (priced[0].get("observed") or {}).get("canonical_name")
    assert name and name in layer, (
        f"the API returned an unsuppressed Tier-"
        f"{priced[0]['price_prediction'].get('evidence_tier')} price for {name!r}"
        " but the UI suppressed the whole competitor layer"
    )
    assert page.locator("#p2wLayerCompetitors .bg-card").count() >= len(priced)
    _shot(page, "09-competitors")


def test_sample_size_agrees_between_the_market_layer_and_the_drilldown(session, rich):
    """The same prediction must report one evidence count, not two.

    Regression guard for the defect fixed in this battery: the chip under the
    range was labelled "حجم العيّنة" but rendered ``evidence.comparable_count``
    (the activity/agency comparable pool), while the "لماذا؟" panel rendered
    ``prediction.evidence_count`` (the comparables that actually entered the
    quantile fit). On tender 1006 those were 9 and 20, so the two surfaces
    disagreed about the evidence behind the very same number.
    """
    page, _ = session
    _run_for(page, rich.tender_id)
    api = page.evaluate("() => p2wState.mi")
    pred = api["prediction"]
    if pred["is_suppressed"]:
        pytest.skip("subject is suppressed; there is no fitted sample to report")

    market = _text(page, "#p2wLayerMarket")
    assert f"{pred['evidence_count']:,}" in market
    assert "حجم العيّنة المحتسبة" in market

    page.locator("#p2wLayerMarket button", has_text="لماذا؟").first.click()
    page.wait_for_selector("#p2wWhyPanel .bg-card", timeout=30_000)
    panel = _text(page, "#p2wWhyPanel")

    def _after(text: str, label: str) -> str:
        i = text.index(label) + len(label)
        return text[i : i + 40]

    drill = _after(panel, "عدد الأدلة")
    chip = _after(market, "حجم العيّنة المحتسبة")
    n = f"{pred['evidence_count']:,}"
    assert n in drill, f"drill-down evidence count {drill!r} != API {n}"
    assert n in chip, f"market chip {chip!r} != API {n}"


# --------------------------------------------------------------------------
# 9. modal semantics — the drawer is a modal surface, keyboard must agree
# --------------------------------------------------------------------------
def _tab_stops(page, n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for _ in range(n):
        page.keyboard.press("Tab")
        r = page.evaluate(
            """() => {
                 const a = document.activeElement;
                 if (!a) return null;
                 const d = document.getElementById('drawer');
                 return {inDrawer: d.contains(a), tag: a.tagName, id: a.id || '',
                         txt: (a.innerText || a.value || '').trim().slice(0, 30)};
               }"""
        )
        if r:
            out.append(r)
    return out


def test_open_drawer_does_not_leak_tab_focus_to_the_page_behind_it(session, rich):
    """Regression guard for a defect found and fixed by this battery.

    The drawer covers the viewport and ``#overlay`` makes everything behind it
    inert to the mouse. Keyboard has to agree. Before the fix, ``openDetail()``
    never moved focus and left the shell tabbable, so Tab walked 32 stops
    through content the user could not see — measured on the live console — and
    the drawer's own controls were never reached.
    """
    page, log = session
    _open_p2w(page)
    page.evaluate(f"() => {{ openDetail({rich.tender_id}); }}")
    page.wait_for_function(
        "() => document.getElementById('drawer').getAttribute('aria-hidden') === 'false'",
        timeout=30_000,
    )

    assert page.evaluate(
        "() => document.getElementById('drawer').contains(document.activeElement)"
    ), "opening the drawer left focus outside it"
    assert page.get_attribute("#drawer", "role") == "dialog"
    assert page.get_attribute("#drawer", "aria-modal") == "true"
    assert page.get_attribute("#appShell", "inert") is not None, (
        "the page behind an open modal drawer is still in the tab order"
    )

    escapees = [s for s in _tab_stops(page, 40) if not s["inDrawer"] and s["tag"] != "BODY"]
    assert escapees == [], (
        f"Tab escaped the open drawer into the page behind it: {escapees[:5]}"
    )
    _shot(page, "10-drawer-focus")
    page.keyboard.press("Escape")
    assert log.js == [], f"JS console errors driving the modal drawer: {log.js}"


def test_closed_drawer_holds_no_focusable_controls(session, rich):
    """Regression guard: aria-hidden-focus.

    A closed drawer is only translated off-screen, so its controls stayed in the
    tab order while sitting inside an ``aria-hidden="true"`` subtree. Measured on
    the live console: 7 focusable descendants, and tabbing from the top of the
    page really did land on three of them (the export link, "+ غرفة العمليات"
    and the ✕), sending the user's focus into a panel they cannot see.
    """
    page, _ = session
    _open_p2w(page)
    # Populate the drawer first: an empty drawer would pass this vacuously.
    page.evaluate(f"() => {{ openDetail({rich.tender_id}); }}")
    page.wait_for_function(
        "() => document.getElementById('drawer').getAttribute('aria-hidden') === 'false'",
        timeout=30_000,
    )
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => document.getElementById('drawer').getAttribute('aria-hidden') === 'true'",
        timeout=10_000,
    )

    populated = page.evaluate(
        """() => document.getElementById('drawer').querySelectorAll(
             'a[href],button,input,select,textarea,[tabindex]:not([tabindex="-1"])').length"""
    )
    assert populated >= 1, "drawer never rendered controls; this guard would be vacuous"
    assert page.get_attribute("#drawer", "inert") is not None, (
        "an aria-hidden drawer that is not inert keeps its controls tabbable"
    )
    assert page.get_attribute("#appShell", "inert") is None, (
        "closing the drawer must give the page behind it back to the keyboard"
    )

    page.evaluate("() => document.body.focus()")
    page.keyboard.press("Home")
    inside = [s for s in _tab_stops(page, 220) if s["inDrawer"]]
    assert inside == [], f"Tab landed inside the closed aria-hidden drawer: {inside[:3]}"


def test_escape_returns_focus_to_the_control_that_opened_the_drawer(session, rich):
    page, _ = session
    _open_p2w(page)
    page.wait_for_selector("#p2wSearchBtn")
    page.focus("#p2wSearchBtn")
    page.evaluate(f"() => {{ openDetail({rich.tender_id}); }}")
    page.wait_for_function(
        "() => document.getElementById('drawer').getAttribute('aria-hidden') === 'false'",
        timeout=30_000,
    )
    # Without this the test is vacuous: if opening never moved focus, focus is
    # still on the opener and Escape "returns" it by doing nothing.
    assert page.evaluate("() => document.activeElement && document.activeElement.id") != (
        "p2wSearchBtn"
    ), "opening the drawer never moved focus, so the return-focus check proves nothing"
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => document.getElementById('drawer').getAttribute('aria-hidden') === 'true'",
        timeout=10_000,
    )
    assert page.evaluate("() => document.activeElement && document.activeElement.id") == (
        "p2wSearchBtn"
    ), "closing the drawer dropped focus instead of returning it to the opener"


# --------------------------------------------------------------------------
# 10. accessible names and live regions
# --------------------------------------------------------------------------
def test_every_p2w_control_has_an_accessible_name(session, rich):
    """A placeholder is not a label (WCAG 4.1.2).

    ``#p2wSearch`` — the entry point of the whole workflow — had only a
    placeholder, so it was announced as an unlabelled search box.
    """
    page, _ = session
    _run_for(page, rich.tender_id)
    gaps = page.evaluate(
        """() => {
             const out = {buttons: [], fields: []};
             document.querySelectorAll('#view-p2w button').forEach(b => {
               const n = (b.innerText || '').trim() || b.getAttribute('aria-label') || b.title;
               if (!n) out.buttons.push(b.outerHTML.slice(0, 80));
             });
             document.querySelectorAll('#view-p2w input, #view-p2w select').forEach(i => {
               const labelled = (i.labels && i.labels.length) || i.getAttribute('aria-label')
                 || i.getAttribute('aria-labelledby');
               if (!labelled) out.fields.push(i.id || i.name || i.type);
             });
             return out;
           }"""
    )
    assert gaps["buttons"] == [], f"buttons with no accessible name: {gaps['buttons']}"
    assert gaps["fields"] == [], f"form fields with no accessible name: {gaps['fields']}"


def test_the_why_panel_is_an_announced_live_region(session, rich):
    """The drill-down loads asynchronously into a div that is otherwise silent."""
    page, _ = session
    _run_for(page, rich.tender_id)
    assert page.get_attribute("#p2wWhyPanel", "aria-live") == "polite", (
        "the لماذا؟ panel fills in after a fetch; without aria-live nothing is announced"
    )
    assert page.get_attribute("#p2wWhyPanel", "role") == "region"
    assert page.get_attribute("#p2wWhyPanel", "aria-label")


# --------------------------------------------------------------------------
# 11. provenance across the WHOLE surface, not just the bid layer
# --------------------------------------------------------------------------
def test_every_number_on_every_layer_carries_a_provenance_badge(session, rich):
    """House rule 1 applies to the whole P2W surface.

    ``test_every_headline_number_carries_a_provenance_badge`` scans only
    ``#p2wBidResult``. This widens the same quantity rule to all three layers so
    an unattributed number in the market or competitor layer cannot hide.

    The attribution may sit on any ancestor inside the layer (the observed-facts
    block and the comparable-tender ``<details>`` each carry one badge that
    governs the table beneath it), which is why this walks ancestors rather than
    demanding a badge in the nearest card.
    """
    page, _ = session
    _run_for(page, rich.tender_id)
    page.fill("#p2wCost", str(max(1000, int((rich.quantiles.get("p10") or 50_000) * 0.6))))
    page.click("#p2wCalcBtn")
    page.wait_for_selector("#p2wBidResult .num", timeout=60_000)

    orphans = page.evaluate(
        """() => {
             const QUANTITY = /^-?[\\d][\\d,\\.\\u066B\\u066C]*\\s*(%|ر\\.س)?$/u;
             const LAYERS = ['#p2wLayerMarket', '#p2wLayerCompetitors', '#p2wLayerBid'];
             const out = [];
             LAYERS.forEach(sel => {
               const layer = document.querySelector(sel);
               if (!layer) return;
               layer.querySelectorAll('.num').forEach(n => {
                 const txt = n.textContent.replace(/\\s+/g, ' ').trim();
                 if (!QUANTITY.test(txt)) return;   // identifiers / prose, not amounts
                 for (let el = n; el && el !== layer.parentElement; el = el.parentElement) {
                   if (el.querySelector &&
                       el.querySelector('.prov-observed,.prov-predicted,.prov-user')) return;
                 }
                 out.push(sel + ' :: ' + txt);
               });
             });
             return out.slice(0, 20);
           }"""
    )
    assert orphans == [], f"numbers with no provenance attribution anywhere above them: {orphans}"


# --------------------------------------------------------------------------
# authentication gate
# --------------------------------------------------------------------------
def test_login_gate_blocks_data_until_signed_in_and_logout_ends_it(_pw):
    """Fresh browser: the gate is up and no data endpoint answers 200. Signing
    in through the form loads the dashboard; logging out puts the gate back and
    the old cookie no longer works."""
    user, password = credential("THAQIP_ADMIN_USER"), credential("THAQIP_ADMIN_PASSWORD")
    if not (user and password):
        pytest.skip("no admin credential (var/credentials.env)")
    ctx = _pw.new_context(viewport=DESKTOP, locale="ar-SA")
    page = ctx.new_page()
    data_ok: list[str] = []
    page.on("response", lambda r: data_ok.append(r.url) if (
        "/api/" in r.url and "/api/auth/" not in r.url and r.status == 200) else None)
    try:
        page.goto(API + "/", wait_until="networkidle")
        assert page.is_visible("#loginForm")
        assert data_ok == [], f"data served before login: {data_ok}"

        page.fill("#loginUser", user)
        page.fill("#loginPass", "definitely-wrong")
        page.click("#loginBtn")
        page.wait_for_function("document.getElementById('loginErr').textContent.length > 0")
        assert page.is_visible("#loginForm")

        page.fill("#loginPass", password)
        page.click("#loginBtn")
        page.wait_for_function(
            "document.getElementById('scOpen').textContent.trim() !== '—'", timeout=20000)
        assert page.is_hidden("#loginGate")
        assert page.text_content("#whoName") == user
        cookie = next(c for c in ctx.cookies() if c["name"] == "thaqip_session")
        assert cookie["httpOnly"]

        page.click("#logoutBtn")
        page.wait_for_selector("#loginGate:not([hidden])")
        replay = urllib.request.Request(
            API + "/api/stats", headers={"Cookie": f"thaqip_session={cookie['value']}"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(replay, timeout=10)
        assert exc.value.code == 401, "a logged-out session cookie still works"
    finally:
        ctx.close()
