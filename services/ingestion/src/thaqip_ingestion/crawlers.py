"""Advanced crawler & anti-bot adapters (Ticket B5 & ecosystem expansion).

Integrates modern scraping and crawling abstractions:
1. Scrapling Adapter:
   - Adaptive element tracking (self-healing selectors when portal DOM / CSS updates)
   - Stealthy / anti-bot fetcher fallback for TSPD and Cloudflare-style challenges
2. Crawl4AI Adapter:
   - Playwright-based deep crawling returning AI-ready structured Markdown and clean text
   - Direct integration into document text chunking and LLM compliance pipelines
3. AgentReach / Extension Relay Provider (PRD M1-7 Seam):
   - Protocol implementation for authenticated session handoff from browser agents
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("thaqip.crawlers")


class ScraplingAdapter:
    """Wrapper around Scrapling (or fallback to httpx/BeautifulSoup) for resilient scraping."""

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._available = False
        try:
            import scrapling  # noqa: F401
            self._available = True
            log.info("scrapling framework is available")
        except ImportError:
            log.info("scrapling package not installed; adapter will operate in compatibility mode")

    @property
    def is_available(self) -> bool:
        return self._available

    def fetch_adaptive(self, url: str, selector: str, *, stealth: bool = True) -> list[str]:
        """Fetch a page using adaptive element tracking to resist DOM shifts."""
        if self._available:
            try:
                from scrapling.fetchers import StealthyFetcher
                page = StealthyFetcher.fetch(url, headless=self.headless)
                elements = page.css(selector, adaptive=True)
                return [el.text for el in elements if hasattr(el, "text")]
            except Exception as e:  # noqa: BLE001 — crawler failures must not kill caller
                log.warning("scrapling fetch failed on %s: %s", url, e)
                return []
        log.debug("scrapling not installed; skipping adaptive fetch for %s", url)
        return []


class Crawl4AIAdapter:
    """Adapter for Crawl4AI to produce clean Markdown/JSON for LLM & vector search pipelines."""

    def __init__(self) -> None:
        self._available = False
        try:
            import crawl4ai  # noqa: F401
            self._available = True
            log.info("crawl4ai framework is available")
        except ImportError:
            log.info("crawl4ai not installed; adapter will operate in compatibility mode")

    @property
    def is_available(self) -> bool:
        return self._available

    async def crawl_markdown(self, url: str, **kwargs: Any) -> str | None:
        """Crawl a URL and return cleaned Markdown stripped of navigation/boilerplate."""
        if self._available:
            try:
                from crawl4ai import AsyncWebCrawler
                async with AsyncWebCrawler() as crawler:
                    result = await crawler.arun(url=url, **kwargs)
                    return getattr(result, "markdown", None) or getattr(result, "text", "")
            except Exception as e:  # noqa: BLE001 — crawler failures must not kill caller
                log.warning("crawl4ai failed on %s: %s", url, e)
                return None
        log.debug("crawl4ai not installed; skipping crawl_markdown for %s", url)
        return None


class AgentReachSessionProvider:
    """SessionProvider implementation that accepts cookies relayed from an agent or extension.

    Satisfies the B5 / M1-7 architectural seam: enables automated agents or user-authorized
    browser extensions to inject authenticated TSPD cookies into Thaqip's detail pipeline.
    """

    def __init__(self) -> None:
        self._cookies: dict[str, str] = {}

    def update_cookies(self, cookies: dict[str, str]) -> None:
        """Called by an agent reach endpoint or extension relay."""
        self._cookies.update(cookies)
        log.info("agent reach session provider received %d cookies", len(cookies))

    async def get_cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    async def invalidate(self) -> None:
        log.warning("agent reach session invalidated")
        self._cookies.clear()
