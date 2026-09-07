import pytest

from thaqip_ingestion.crawlers import (
    AgentReachSessionProvider,
    Crawl4AIAdapter,
    ScraplingAdapter,
)
from thaqip_ingestion.etimad.session import SessionProvider


def test_scrapling_adapter_initialization():
    adapter = ScraplingAdapter()
    assert isinstance(adapter.is_available, bool)
    res = adapter.fetch_adaptive("https://example.com", ".item")
    assert isinstance(res, list)


@pytest.mark.asyncio
async def test_crawl4ai_adapter_initialization():
    adapter = Crawl4AIAdapter()
    assert isinstance(adapter.is_available, bool)
    res = await adapter.crawl_markdown("https://example.com")
    assert res is None or isinstance(res, str)


@pytest.mark.asyncio
async def test_agent_reach_session_provider_protocol():
    provider = AgentReachSessionProvider()
    # verify it satisfies SessionProvider protocol
    assert isinstance(provider, SessionProvider)

    cookies = await provider.get_cookies()
    assert cookies == {}

    provider.update_cookies({"TSPD_101": "token123", "session": "abc"})
    cookies = await provider.get_cookies()
    assert cookies.get("TSPD_101") == "token123"

    await provider.invalidate()
    cookies = await provider.get_cookies()
    assert cookies == {}
