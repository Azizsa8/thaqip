"""Build a static, shareable demo of the console for Netlify.

Bakes live API responses into data/db.json, copies the SPA, and injects
demo-shim.js which (1) gates the page behind a password and (2) intercepts
fetch('/api/...') to serve everything client-side from the snapshot —
filters, pagination, and detail views keep working; mutations apply
in-memory only. No backend, no credentials, nothing live is exposed.

Run:  uv run python tools/export_demo.py  (console must be up on :8091)
"""
from __future__ import annotations

import json
import pathlib
import urllib.request

BASE = "http://localhost:8091"
OUT = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "netlify"
REPO = pathlib.Path(__file__).resolve().parent.parent


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.load(r)


def main() -> None:
    (OUT / "data").mkdir(parents=True, exist_ok=True)

    tenders = get("/api/tenders?limit=200")["items"]
    tender_details = {}
    detail_ids = {t["id"] for t in tenders[:60]}
    # make sure awarded + pursuit + forsah tenders have details in the snapshot
    for t in get("/api/tenders?limit=60&awarded=true")["items"]:
        detail_ids.add(t["id"])
    for t in get("/api/tenders?limit=40&source=forsah")["items"]:
        tenders.append(t)
        detail_ids.add(t["id"])
    pursuits = get("/api/pursuits")
    for p in pursuits:
        detail_ids.add(p["tender_id"])
    for tid in detail_ids:
        try:
            tender_details[str(tid)] = get(f"/api/tenders/{tid}")
        except Exception:
            pass

    agencies_board = get("/api/agencies?limit=80")
    agency_details = {}
    for a in agencies_board[:30]:
        agency_details[str(a["id"])] = get(f"/api/agencies/{a['id']}")

    vendors = get("/api/vendors?limit=100")
    vendor_details = {}
    for v in vendors[:40]:
        vendor_details[str(v["id"])] = get(f"/api/vendors/{v['id']}")

    db = {
        "generated_note": "نسخة عرض ثابتة — البيانات لحظة التصدير من بيئة التطوير",
        "dashboard": get("/api/dashboard"),
        "filters": get("/api/filters"),
        "tenders": tenders,
        "tender_details": tender_details,
        "vendors": vendors,
        "vendor_details": vendor_details,
        "agencies_board": agencies_board,
        "agency_details": agency_details,
        "pursuits": pursuits,
        "pursuit_details": {str(p["id"]): get(f"/api/pursuits/{p['id']}") for p in pursuits},
        "profiles": get("/api/profiles"),
        "notifications": get("/api/notifications?limit=60"),
        "pricing_accuracy": get("/api/pricing/accuracy"),
    }
    (OUT / "data" / "db.json").write_text(json.dumps(db, ensure_ascii=False, default=str))

    html = (REPO / "services/console/src/thaqip_console/static/index.html").read_text()
    html = html.replace("<body ", '<body data-demo="1" ', 1)
    html = html.replace(
        "<script>\nconst $ =",
        '<script src="demo-shim.js"></script>\n<script>\nconst $ =', 1)
    (OUT / "index.html").write_text(html)
    print("exported:", OUT, "| tenders:", len(tenders), "| details:", len(tender_details),
          "| vendors:", len(vendors))


if __name__ == "__main__":
    main()
