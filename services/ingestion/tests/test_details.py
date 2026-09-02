from pathlib import Path

from thaqip_ingestion.etimad.details import parse_attachment_links, parse_fragment

FIX = Path(__file__).parent / "fixtures"


def test_dates_fragment_parses_labels():
    fields = parse_fragment((FIX / "comp_GetTenderDatesViewComponenet.html").read_text())
    assert fields, "expected label/value pairs from the dates component"
    labels = " ".join(fields)
    assert "آخر موعد لتقديم العروض" in labels
    assert "تاريخ فتح العروض" in labels
    # values carry Gregorian dates
    offers = next(v for k, v in fields.items() if "لتقديم العروض" in k)
    assert "2026" in offers or "1448" in offers


def test_relations_fragment_parses_activity():
    fields = parse_fragment((FIX / "comp_GetRelationsDetailsViewComponenet.html").read_text())
    joined = " ".join(list(fields.keys()) + list(fields.values()))
    assert "تقنية المعلومات" in joined  # tender activity from the live fixture


def test_awarding_not_announced_detected():
    import html

    raw = (FIX / "comp_GetAwardingResultsForVisitorViewComponenet.html").read_text()
    assert "لم يتم اعلان" in html.unescape(raw)  # fixture tender has no award yet


def test_attachment_links_ignore_javascript():
    links = parse_attachment_links((FIX / "comp_GetAttachmentsViewComponenet.html").read_text())
    assert all("javascript" not in l.lower() for l in links)
