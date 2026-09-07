from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from thaqip_ingestion.audit import run_audit
from thaqip_ingestion.etimad.client import EtimadClient

SAMPLE_LISTING_PAGE = {
    "totalCount": 2,
    "pageSize": 50,
    "currentPage": 1,
    "data": [
        {
            "tenderId": 1001,
            "referenceNumber": "REF-01",
            "tenderName": "مشروع توريد 1",
            "tenderIdString": "enc1",
        },
        {
            "tenderId": 1002,
            "referenceNumber": "REF-02",
            "tenderName": "مشروع توريد 2",
            "tenderIdString": "enc2",
        },
    ],
}


@pytest.mark.asyncio
@respx.mock
async def test_audit_capture_rate_success():
    respx.get("https://tenders.etimad.sa/Tender/AllSupplierTendersForVisitorAsync").mock(
        return_value=httpx.Response(200, json=SAMPLE_LISTING_PAGE)
    )

    client = EtimadClient()
    mock_pool = AsyncMock()

    # Simulate that 1001 exists in DB, 1002 is missing
    mock_pool.fetch.return_value = [{"source_tender_id": 1001}]
    mock_pool.execute.return_value = None

    try:
        report = await run_audit(mock_pool, client, sample_size=2)
        assert report["sample_size"] == 2
        assert report["captured"] == 1
        assert report["missing"] == 1
        assert report["capture_rate"] == 0.5
        assert report["criterion_passed"] is False
        assert len(report["missing_samples"]) == 1
        assert report["missing_samples"][0]["tender_id"] == 1002
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_audit_capture_rate_full_pass():
    respx.get("https://tenders.etimad.sa/Tender/AllSupplierTendersForVisitorAsync").mock(
        return_value=httpx.Response(200, json=SAMPLE_LISTING_PAGE)
    )

    client = EtimadClient()
    mock_pool = AsyncMock()

    # Simulate all exist in DB
    mock_pool.fetch.return_value = [{"source_tender_id": 1001}, {"source_tender_id": 1002}]
    mock_pool.execute.return_value = None

    try:
        report = await run_audit(mock_pool, client, sample_size=2)
        assert report["captured"] == 2
        assert report["missing"] == 0
        assert report["capture_rate"] == 1.0
        assert report["criterion_passed"] is True
    finally:
        await client.aclose()
