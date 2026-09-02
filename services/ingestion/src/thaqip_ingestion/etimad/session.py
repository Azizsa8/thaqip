"""TSPD session bootstrap (ticket B2, seam for B5).

Etimad's detail routes sit behind an F5/TSPD JavaScript challenge. Strategy:
solve it ONCE in a real headless browser, export the resulting cookies into
the fast httpx client, and refresh only when a challenge reappears. This
keeps browser usage to a handful of loads per session lifetime instead of
per request.

The SessionProvider protocol is the B5/M1-7 seam: a future implementation can
supply cookies relayed from a user-authorized browser extension instead.
"""
from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)

BOOTSTRAP_URL = "https://tenders.etimad.sa/Tender/AllTendersForVisitor"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class SessionProvider(Protocol):
    async def get_cookies(self) -> dict[str, str]:
        """Return cookies that satisfy the source's bot protection."""
        ...

    async def invalidate(self) -> None:
        """Called when a challenge is seen despite the cookies."""
        ...


class PlaywrightSessionProvider:
    """Solves the TSPD challenge in headless Chromium and caches the cookies."""

    def __init__(self, *, headless: bool = True, settle_ms: int = 6000) -> None:
        self._headless = headless
        self._settle_ms = settle_ms
        self._cookies: dict[str, str] | None = None

    async def get_cookies(self) -> dict[str, str]:
        if self._cookies is None:
            self._cookies = await self._bootstrap()
        return self._cookies

    async def invalidate(self) -> None:
        log.warning("session invalidated; will re-bootstrap on next request")
        self._cookies = None

    async def _bootstrap(self) -> dict[str, str]:
        from playwright.async_api import async_playwright

        log.info("bootstrapping Etimad session via headless browser")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self._headless)
            try:
                context = await browser.new_context(user_agent=USER_AGENT, locale="ar-SA")
                page = await context.new_page()
                await page.goto(BOOTSTRAP_URL, wait_until="domcontentloaded", timeout=60_000)
                # TSPD reloads the page after solving; give it time to settle.
                await page.wait_for_timeout(self._settle_ms)
                cookies = {c["name"]: c["value"] for c in await context.cookies()}
                tspd = [n for n in cookies if n.startswith("TS")]
                log.info("bootstrap complete: %d cookies (%d TSPD)", len(cookies), len(tspd))
                if not tspd:
                    raise RuntimeError("bootstrap produced no TSPD cookies; challenge unsolved")
                return cookies
            finally:
                await browser.close()
