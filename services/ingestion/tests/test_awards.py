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


# --------------------------------------------------------------------------
# fresh vs backfill lanes
# --------------------------------------------------------------------------
class _CursorPool(_FakePool):
    """Serves a stored checkpoint and records every page the harvester asks for."""

    def __init__(self, last_page):
        super().__init__()
        self.last_page = last_page

    async def fetchval(self, query, *args):
        if "INSERT INTO ingest_runs" in query:
            return 123
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if "SELECT checkpoint" in query:
            return {"checkpoint": {"last_page": self.last_page}}
        raise AssertionError(query)


def _cooloff():
    request = httpx.Request("GET", "https://tenders.etimad.sa/x")
    return httpx.HTTPStatusError("waf cool-off", request=request,
                                 response=httpx.Response(400, request=request))


@pytest.mark.asyncio
async def test_fresh_lane_always_starts_at_the_newest_page(monkeypatch):
    asked = []

    async def _empty(self, client, page, page_size, extra):
        asked.append(page)
        return []

    monkeypatch.setattr(AwardsHarvester, "_fetch_awarded_page", _empty)
    await AwardsHarvester(_CursorPool(last_page=4000), _FakeSession()).run(pages=5)
    assert asked == [1], "fresh harvest resumed deep and would miss new awards"


@pytest.mark.asyncio
async def test_backfill_resumes_and_a_cooloff_keeps_completed_pages(monkeypatch):
    """The old code rewrote the checkpoint to start_page-1 on cool-off, so every
    WAF pause rewound the walk and history never grew."""
    asked = []

    async def _rows(self, client, page, page_size, extra):
        asked.append(page)
        if page >= 43:
            raise _cooloff()
        return [_Row()]

    class _Row:
        tender_id = 1
        tender_id_string = ""   # no awarding fetch

    async def _upsert(pool, row, detected_by):
        return None

    monkeypatch.setattr(AwardsHarvester, "_fetch_awarded_page", _rows)
    monkeypatch.setattr("thaqip_ingestion.awards_harvest.db.upsert_tender", _upsert)

    class _Pool(_CursorPool):
        async def fetchval(self, query, *args):
            if "SELECT id FROM tenders" in query:
                return 99
            return await super().fetchval(query, *args)

    pool = _Pool(last_page=40)
    stats = await AwardsHarvester(pool, _FakeSession()).run(pages=10, mode="backfill")

    assert asked[0] == 41, "backfill did not resume from its checkpoint"
    assert stats["cooldown"] is True and stats["pages"] == 2
    final = [u for u in pool.updates if "checkpoint=$2::jsonb" in u[0]][-1]
    assert '"last_page": 42' in final[1][1], f"cool-off rewound the cursor: {final[1][1]}"


@pytest.mark.asyncio
async def test_backfilled_awards_never_reach_customer_alerts():
    from thaqip_ingestion import alerts

    class _NoDbPool:
        async def fetchrow(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("historical event was processed for alerts")

        fetch = fetchval = fetchrow

    fields = {"event_type": "tender.awarded", "entity_type": "tender", "event_id": "1",
              "entity_id": "5", "data": '{"backfill": true, "awardees": ["x"]}'}
    assert await alerts.handle(_NoDbPool(), sender=None, fields=fields) == 0
