import json
from pathlib import Path

from thaqip_ingestion.forsah import content_hash, normalize

FIX = Path(__file__).parent / "fixtures" / "forsah_page.json"


def rows():
    return json.loads(FIX.read_text())["result"]


def test_normalize_maps_core_fields():
    c = normalize(rows()[0])
    assert c["source"] == "forsah"
    assert len(c["source_uid"]) == 36  # uuid
    assert c["name"]
    assert c["status_id"] in (0, 4, 8, 15)
    assert c["last_offer_date"]  # dueDate present on open RFQs


def test_intensity_counters_present():
    c = normalize(rows()[0])
    for k in ("bids_count", "submitted_bids_count", "external_bids_count", "draft_bids_count"):
        assert c[k] is not None


def test_hash_stable_and_sensitive():
    r = rows()[0]
    a, b = content_hash(normalize(r)), content_hash(normalize(r))
    assert a == b
    r2 = dict(r, submittedBidsCount=(r.get("submittedBidsCount") or 0) + 1)
    assert content_hash(normalize(r2)) != a
