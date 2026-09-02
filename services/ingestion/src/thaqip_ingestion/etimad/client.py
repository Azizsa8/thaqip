"""Etimad visitor-API client (ticket B1).

Verified working endpoint (2026-09-02):
  GET https://tenders.etimad.sa/Tender/AllSupplierTendersForVisitorAsync
      ?PageSize=<n>&PageNumber=<n>            -> JSON, no auth required.

Some sibling routes (e.g. AllTendersForVisitorAsync) return an F5/TSPD
JavaScript challenge instead of JSON. `looks_like_challenge` detects that
so callers never mistake challenge HTML for data (feeds ticket B5).
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator

import httpx

from .models import EtimadListingPage, EtimadTenderRow

log = logging.getLogger(__name__)

BASE_URL = "https://tenders.etimad.sa"
LISTING_PATH = "/Tender/AllSupplierTendersForVisitorAsync"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ar,en;q=0.8",
}


class ChallengeDetected(Exception):
    """The response is a bot-protection challenge, not data."""


def looks_like_challenge(body: bytes) -> bool:
    head = body[:2048]
    return b"TSPD" in head or b"bobcmn" in head or head.lstrip().startswith(b"<!DOCTYPE")


class EtimadClient:
    """Rate-limited, retrying client for Etimad's public visitor endpoints."""

    def __init__(
        self,
        *,
        rate_limit_per_sec: float = 1.0,
        max_retries: int = 5,
        timeout: float = 30.0,
    ) -> None:
        self._min_interval = 1.0 / rate_limit_per_sec
        self._max_retries = max_retries
        self._last_request_at = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=BASE_URL, headers=DEFAULT_HEADERS, timeout=timeout, http2=True
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _throttled_get(self, path: str, params: dict[str, object]) -> httpx.Response:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = asyncio.get_event_loop().time()
        return await self._http.get(path, params=params)

    async def fetch_listing_page(self, page: int, page_size: int = 50) -> EtimadListingPage:
        params = {"PageSize": page_size, "PageNumber": page}
        delay = 1.0
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await self._throttled_get(LISTING_PATH, params)
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 60)) + 30 * attempt
                    log.warning("429 rate limit; honoring backoff %.0fs (attempt %s)", wait, attempt)
                    await asyncio.sleep(wait)
                    raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
                if resp.status_code in (502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                if looks_like_challenge(resp.content):
                    raise ChallengeDetected(LISTING_PATH)
                return EtimadListingPage.model_validate_json(resp.content)
            except ChallengeDetected:
                raise  # circuit-breaker territory (B5) — never retry blindly into a challenge
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == self._max_retries:
                    raise
                sleep_for = delay + random.uniform(0, delay / 2)
                log.warning(
                    "etimad listing page=%s attempt=%s failed (%s); retrying in %.1fs",
                    page, attempt, exc, sleep_for,
                )
                await asyncio.sleep(sleep_for)
                delay = min(delay * 2, 60)
        raise RuntimeError("unreachable")

    async def iter_newest(
        self, *, max_pages: int, page_size: int = 50
    ) -> AsyncIterator[EtimadTenderRow]:
        """Yield rows from the newest-first listing (fast lane of ticket D2)."""
        for page in range(1, max_pages + 1):
            listing = await self.fetch_listing_page(page, page_size)
            for row in listing.data:
                yield row
            if page * page_size >= listing.totalCount:
                return
