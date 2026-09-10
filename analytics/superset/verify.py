"""Open every Superset chart in Explore and fail on any error.

The REST API accepts form_data that the React query builders later reject
(bubble_v2's orderby is one), and those errors only surface in the browser —
where one broken chart can blank a whole dashboard tab. So verification is a
real browser: log in, load each chart, watch /api/v1/chart/data and page
errors, read any error alert.

    cd services/ingestion && uv run --extra browser python ../../analytics/superset/verify.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from provision import Superset, _env  # noqa: E402


def main() -> int:
    env = _env()
    base = env["SUPERSET_URL"]
    ss = Superset(base, env["SUPERSET_ADMIN_USER"], env["SUPERSET_ADMIN_PASSWORD"])
    charts = sorted((c["id"], c["viz_type"], c["slice_name"]) for c in ss.all("chart"))
    bad = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(base + "/login/")
        page.fill("#username", env["SUPERSET_ADMIN_USER"])
        page.fill("#password", env["SUPERSET_ADMIN_PASSWORD"])
        page.keyboard.press("Enter")
        page.wait_for_url(lambda u: "/login" not in u, timeout=30000)
        for cid, viz, name in charts:
            problems: list[str] = []

            def on_response(r, problems=problems):
                if "/api/v1/chart/data" in r.url and r.status >= 400:
                    problems.append(f"HTTP {r.status}: {r.text()[:200]}")

            def on_error(e, problems=problems):
                problems.append(f"page error: {str(e)[:200]}")

            page.on("response", on_response)
            page.on("pageerror", on_error)
            page.goto(f"{base}/explore/?slice_id={cid}", wait_until="networkidle")
            page.wait_for_timeout(3500)
            problems += [t.strip()[:200] for t in page.locator(
                "[data-test='error-message'], .ant-alert-error").all_inner_texts() if t.strip()]
            page.remove_listener("response", on_response)
            page.remove_listener("pageerror", on_error)
            bad += bool(problems)
            print(f"{'BAD' if problems else 'ok '} {cid:>4} {viz:<26} {name}")
            for line in problems:
                print(f"       {line}")
        browser.close()
    print(f"{len(charts) - bad}/{len(charts)} charts render")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
