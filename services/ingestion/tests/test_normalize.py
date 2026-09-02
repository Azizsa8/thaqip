import json
from pathlib import Path

from thaqip_ingestion.etimad.models import EtimadListingPage
from thaqip_ingestion.normalize import classify_change, content_hash, diff_fields, to_canonical

FIXTURE = Path(__file__).parent / "fixtures" / "listing_page.json"


def load_page() -> EtimadListingPage:
    return EtimadListingPage.model_validate(json.loads(FIXTURE.read_text()))


def test_fixture_parses():
    page = load_page()
    assert page.totalCount > 0
    assert page.data, "fixture should contain tender rows"
    row = page.data[0]
    assert row.tender_id > 0
    assert row.reference_number
    assert row.tender_name


def test_canonicalization_is_idempotent():
    row = load_page().data[0]
    c1, c2 = to_canonical(row), to_canonical(row)
    assert c1 == c2
    assert content_hash(c1) == content_hash(c2)


def test_diff_detects_extension():
    row = load_page().data[0]
    old = to_canonical(row)
    new = dict(old, last_offer_date="2026-12-31T09:59:00")
    changed = diff_fields(old, new)
    assert changed == ["last_offer_date"]
    assert classify_change(changed) == "tender.extended"


def test_unchanged_row_diffs_empty():
    row = load_page().data[0]
    assert diff_fields(to_canonical(row), to_canonical(row)) == []
