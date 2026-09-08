from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from thaqip_ingestion.awards_harvest import AwardsHarvester
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


class _FakePool:
    def __init__(self):
        self.updates = []

    async def fetchval(self, query, *args):
        if "INSERT INTO ingest_runs" in query:
            return 123
        if "SELECT checkpoint" in query:
            return
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if "SELECT checkpoint" in query:
            return
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.updates.append((query, args))


class _FakeSession:
    async def fetch_component(self, source_id, component):  # pragma: no cover - not reached
        raise AssertionError("award component should not be fetched during listing cool-off")

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_awards_harvest_treats_waf_cooloff_as_scheduled_cooldown(monkeypatch):
    async def _raise_cooloff(self, client, page, page_size, extra):
        request = httpx.Request("GET", "https://tenders.etimad.sa/Tender/AllSupplierTendersForVisitorAsync")
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError("waf cool-off", request=request, response=response)

    monkeypatch.setattr(AwardsHarvester, "_fetch_awarded_page", _raise_cooloff)
    pool = _FakePool()
    harvester = AwardsHarvester(pool, _FakeSession())

    stats = await harvester.run(pages=1)

    assert stats["cooldown"] is True
    checkpoint_updates = [u for u in pool.updates if "checkpoint=$2::jsonb" in u[0]]
    assert checkpoint_updates
    assert '"cooldown": true' in checkpoint_updates[-1][1][1]
    finish_updates = [u for u in pool.updates if "finished_at=now()" in u[0]]
    assert finish_updates
    _, args = finish_updates[-1]
    assert args == (123, True, "waf cool-off")
