from decimal import Decimal
from pathlib import Path

from thaqip_ingestion.etimad.awards import parse_awarding_fragment

FIX = Path(__file__).parent / "fixtures"


def test_not_announced_fragment():
    raw = (FIX / "comp_GetAwardingResultsForVisitorViewComponenet.html").read_text()
    result = parse_awarding_fragment(raw)
    assert result.announced is False
    assert result.bidders == [] and result.awardees == []


def test_awarded_fragment_parses_bidders_and_awardees():
    raw = (FIX / "award_1100258.html").read_text()
    result = parse_awarding_fragment(raw)
    assert result.announced is True
    assert len(result.bidders) == 4
    names = [b.name for b in result.bidders]
    assert "شركة بن سواد للتجارة" in names
    b = next(x for x in result.bidders if x.name == "شركة بن سواد للتجارة")
    assert b.offer_value == Decimal("79902.00")
    assert b.technical_result == "مطابق"
    assert len(result.awardees) >= 1
    assert all(a.name for a in result.awardees)


def test_single_bidder_award():
    raw = (FIX / "award_1100283.html").read_text()
    result = parse_awarding_fragment(raw)
    assert result.announced
    assert len(result.bidders) == 1
    assert result.awardees[0].award_value == Decimal("4025.00")
