import io
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import openpyxl
import pytest

from thaqip_ingestion.boq import parse_boq_xlsx, store_boq


def create_sample_boq_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "جدول الكميات"

    # Header in row 2
    ws.append(["مشروع توريد وتجهيز"])
    ws.append(["م", "بيان الأعمال", "الوحدة", "الكمية", "سعر الوحدة", "الإجمالي"])
    ws.append([1, "توريد وتركيب خادم رئيسي", "عدد", 5, 25000, 125000])
    ws.append([2, "توريد محولات شبكة 48 منفذ", "جهاز", 10, 8000, 80000])
    ws.append([3, "كيابل ألياف بصرية بطول 500م", "لفة", 2, 4500, 9000])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_sample_boq_xlsx():
    data = create_sample_boq_xlsx()
    result = parse_boq_xlsx(data)

    assert result.confidence >= 0.7
    assert len(result.items) == 3
    assert result.items[0].description == "توريد وتركيب خادم رئيسي"
    assert result.items[0].unit == "عدد"
    assert result.items[0].qty == Decimal(5)
    assert result.items[1].qty == Decimal(10)


@pytest.mark.asyncio
async def test_store_boq_mock_db():
    data = create_sample_boq_xlsx()
    result = parse_boq_xlsx(data)

    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()

    class AsyncContextManager:
        async def __aenter__(self):
            return mock_conn
        async def __aexit__(self, exc_type, exc, tb):
            pass

    mock_pool.acquire.return_value = AsyncContextManager()
    mock_conn.transaction.return_value = AsyncContextManager()

    count = await store_boq(mock_pool, tender_pk=42, document_id=1, result=result)
    assert count == 3
    assert mock_conn.execute.call_count >= 3
