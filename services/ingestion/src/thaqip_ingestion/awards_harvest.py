"""Awards harvester job (ticket B4).

Pipeline: awarded-tenders listing (TenderCategory=6) -> upsert tender rows ->
fetch awarding view component -> parse bidders/awardees -> upsert vendors,
replace offers/awards -> emit `tender.awarded` outbox event (once per tender).

Run:
  DATABASE_URL=... uv run --extra db --extra browser \
      python -m thaqip_ingestion.awards_harvest --pages 2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

import asyncpg
import httpx

from . import db
from .etimad.awards import AwardingResult, parse_awarding_fragment
from .etimad.client import DEFAULT_HEADERS, EtimadClient
from .etimad.session import PlaywrightSessionProvider, SessionProvider

log = logging.getLogger("thaqip.awards")

AWARDED_CATEGORY_PARAM = {"TenderCategory": 6}  # "تم اعلان الترسية"
AWARDING_COMPONENT = "/Tender/GetAwardingResultsForVisitorViewComponenet"


async def get_or_create_vendor(conn: asyncpg.Connection, name: str) -> int:
    row = await conn.fetchrow(
        """INSERT INTO vendors (canonical_name) VALUES ($1)
           ON CONFLICT (canonical_name) DO UPDATE SET canonical_name = EXCLUDED.canonical_name
           RETURNING id""",
        name.strip(),
    )
    return row["id"]


async def store_awarding(pool: asyncpg.Pool, tender_pk: int, result: AwardingResult) -> bool:
    """Replace offers/awards for a tender. Returns True if a new award event was emitted."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            had_awards = await conn.fetchval(
                "SELECT count(*) FROM awards WHERE tender_id = $1", tender_pk
            )
            await conn.execute("DELETE FROM offers WHERE tender_id = $1", tender_pk)
            await conn.execute("DELETE FROM awards WHERE tender_id = $1", tender_pk)

            awardee_names = {a.name for a in result.awardees}
            for b in result.bidders:
                vid = await get_or_create_vendor(conn, b.name)
                await conn.execute(
                    """INSERT INTO offers (tender_id, vendor_id, vendor_name_raw, offer_value,
                                           is_winner, technical_pass)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    tender_pk, vid, b.name, b.offer_value,
                    b.name in awardee_names,
                    (b.technical_result or "").strip() == "مطابق" or None,
                )
            for a in result.awardees:
                vid = await get_or_create_vendor(conn, a.name)
                await conn.execute(
                    """INSERT INTO awards (tender_id, vendor_id, award_value)
                       VALUES ($1, $2, $3)""",
                    tender_pk, vid, a.award_value if a.award_value is not None else a.offer_value,
                )

            emit = bool(result.awardees) and had_awards == 0
            if emit:
                await conn.execute(
                    """INSERT INTO ingest_events (event_type, entity_type, entity_id, data)
                       VALUES ('tender.awarded', 'tender', $1, $2::jsonb)""",
                    tender_pk,
                    json.dumps({
                        "awardees": [a.name for a in result.awardees],
                        "bidder_count": len(result.bidders),
                    }, ensure_ascii=False),
                )
            return emit


class AwardsHarvester:
    def __init__(self, pool: asyncpg.Pool, session: SessionProvider, *, delay: float = 3.0) -> None:
        self._pool = pool
        self._session = session
        self._delay = delay
        self._http = httpx.AsyncClient(
            base_url="https://tenders.etimad.sa",
            headers={**DEFAULT_HEADERS, "X-Requested-With": "XMLHttpRequest"},
            timeout=60,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_awarding(self, tender_id_string: str) -> AwardingResult:
        for attempt in (1, 2, 3):
            cookies = await self._session.get_cookies()
            resp = await self._http.get(
                AWARDING_COMPONENT, params={"tenderIdStr": tender_id_string}, cookies=cookies
            )
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 30)) + 5 * attempt
                log.warning("429 on awarding component; sleeping %.0fs", wait)
                await asyncio.sleep(wait)
                continue
            if resp.is_redirect:
                # not in an awarding-visible state (e.g. still receiving offers) -> /Home/Error
                return AwardingResult(announced=False)
            resp.raise_for_status()
            if "bobcmn" in resp.text[:4096] or "TSPD_101" in resp.text[:4096]:
                await self._session.invalidate()
                continue
            return parse_awarding_fragment(resp.text)
        raise RuntimeError("awarding component kept hitting bot challenge")

    async def run(self, *, pages: int, page_size: int = 20) -> dict:
        stats = {"tenders": 0, "announced": 0, "offers": 0, "awards": 0, "events": 0}
        client = EtimadClient()
        # awarded-category listing shares the visitor listing endpoint
        client_iter_params = dict(AWARDED_CATEGORY_PARAM)
        try:
            for page in range(1, pages + 1):
                listing = await self._fetch_awarded_page(client, page, page_size, client_iter_params)
                for row in listing:
                    stats["tenders"] += 1
                    await db.upsert_tender(self._pool, row)
                    pk = await self._pool.fetchval(
                        "SELECT id FROM tenders WHERE source='etimad' AND source_tender_id=$1",
                        row.tender_id,
                    )
                    if not row.tender_id_string:
                        continue
                    result = await self.fetch_awarding(row.tender_id_string)
                    if result.announced:
                        stats["announced"] += 1
                        stats["offers"] += len(result.bidders)
                        stats["awards"] += len(result.awardees)
                        if await store_awarding(self._pool, pk, result):
                            stats["events"] += 1
                    await asyncio.sleep(self._delay)
        finally:
            await client.aclose()
        return stats

    @staticmethod
    async def _fetch_awarded_page(client: EtimadClient, page: int, page_size: int, extra: dict):
        # reuse EtimadClient throttling/retry with extra query params
        resp = await client._throttled_get(  # noqa: SLF001 — same-package reuse until B4 refactor
            "/Tender/AllSupplierTendersForVisitorAsync",
            {"PageSize": page_size, "PageNumber": page, **extra},
        )
        resp.raise_for_status()
        from .etimad.models import EtimadListingPage

        return EtimadListingPage.model_validate_json(resp.content).data


async def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="Thaqip awards harvester")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=20)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    pool = await db.connect(os.environ["DATABASE_URL"])
    harvester = AwardsHarvester(pool, PlaywrightSessionProvider())
    try:
        stats = await harvester.run(pages=args.pages, page_size=args.page_size)
        log.info("harvest complete: %s", stats)
    finally:
        await harvester.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
