"""Tender detail fetcher (ticket B2).

The visitor detail page (/Tender/DetailsForVisitor?STenderId=...) is a shell;
the data loads through view-component fragments keyed by `tenderIdStr` (the
raw encrypted id, param name verified from the shell's inline JS, 2026-09-02):

  GET /Tender/GetRelationsDetailsViewComponenet?tenderIdStr=...   classification, location, activity
  GET /Tender/GetTenderDatesViewComponenet?tenderIdStr=...        full date sheet (Greg + Hijri)
  GET /Tender/GetAttachmentsViewComponenet?tenderIdStr=...        attachment list/links
  GET /Tender/GetAwardingResultsForVisitorViewComponenet?...      award results (feeds ticket B4)

All routes sit behind TSPD — requests need cookies from a SessionProvider
(session.py). Fragments are HTML-entity-encoded Arabic; parse_fragment()
extracts label→value pairs from the Bootstrap list-group markup.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import logging
import re
from dataclasses import dataclass, field

import httpx

from .client import DEFAULT_HEADERS
from .session import SessionProvider

log = logging.getLogger(__name__)

BASE_URL = "https://tenders.etimad.sa"

COMPONENTS = {
    "relations": "/Tender/GetRelationsDetailsViewComponenet",
    "dates": "/Tender/GetTenderDatesViewComponenet",
    "attachments": "/Tender/GetAttachmentsViewComponenet",
    "awarding": "/Tender/GetAwardingResultsForVisitorViewComponenet",
}

_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
_ITEM_RE = re.compile(r'<li class="list-group-item">(.*?)</li>', re.S)
_TITLE_RE = re.compile(r'class="[^"]*etd-item-title[^"]*"[^>]*>\s*(.*?)\s*</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r'href="([^"]+)"')
_WS_RE = re.compile(r"\s+")


def _clean(fragment: str) -> str:
    return _WS_RE.sub(" ", html_mod.unescape(_TAG_RE.sub(" ", fragment))).strip()


def parse_fragment(raw_html: str) -> dict[str, str]:
    """Extract label -> value pairs from a view-component fragment."""
    body = _SCRIPT_RE.sub("", raw_html)
    fields: dict[str, str] = {}
    for item in _ITEM_RE.finditer(body):
        block = item.group(1)
        title_m = _TITLE_RE.search(block)
        if not title_m:
            continue
        label = _clean(title_m.group(1))
        value = _clean(block[title_m.end():])
        if label and value:
            fields.setdefault(label, value)
    return fields


def parse_attachment_links(raw_html: str) -> list[str]:
    body = _SCRIPT_RE.sub("", raw_html)
    return list(dict.fromkeys(
        html_mod.unescape(h) for h in _HREF_RE.findall(body) if "javascript" not in h.lower()
    ))


@dataclass
class TenderDetail:
    tender_id_string: str
    relations: dict[str, str] = field(default_factory=dict)
    dates: dict[str, str] = field(default_factory=dict)
    attachments: list[str] = field(default_factory=list)
    awarding: dict[str, str] = field(default_factory=dict)
    awarding_announced: bool = False
    raw: dict[str, str] = field(default_factory=dict)  # component -> raw html (retain for reparse)


def _is_challenge(text: str) -> bool:
    head = text[:4096]
    return "bobcmn" in head or "TSPD_101" in head


class DetailsFetcher:
    def __init__(
        self,
        session_provider: SessionProvider,
        *,
        timeout: float = 45.0,
        inter_request_delay: float = 1.0,
    ) -> None:
        self._session = session_provider
        self._delay = inter_request_delay
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={**DEFAULT_HEADERS, "X-Requested-With": "XMLHttpRequest"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_component(self, path: str, tender_id_string: str) -> str:
        for attempt in (1, 2):
            cookies = await self._session.get_cookies()
            resp = await self._http.get(
                path, params={"tenderIdStr": tender_id_string}, cookies=cookies
            )
            resp.raise_for_status()
            if _is_challenge(resp.text):
                log.warning("challenge on %s (attempt %d)", path, attempt)
                await self._session.invalidate()
                continue
            return resp.text
        raise RuntimeError(f"component kept hitting bot challenge: {path}")

    async def fetch(self, tender_id_string: str) -> TenderDetail:
        detail = TenderDetail(tender_id_string=tender_id_string)
        for name, path in COMPONENTS.items():
            raw_html = await self._get_component(path, tender_id_string)
            detail.raw[name] = raw_html
            if name == "attachments":
                detail.attachments = parse_attachment_links(raw_html)
            elif name == "awarding":
                detail.awarding = parse_fragment(raw_html)
                detail.awarding_announced = "لم يتم اعلان" not in _clean(raw_html)
            else:
                setattr(detail, name, parse_fragment(raw_html))
            await asyncio.sleep(self._delay)
        return detail
