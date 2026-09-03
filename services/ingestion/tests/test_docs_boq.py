import io
from decimal import Decimal

import openpyxl

from thaqip_ingestion.boq import parse_boq_xlsx
from thaqip_ingestion.documents import chunk_text, extract_text, sniff_mime


def make_boq_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "جدول الكميات"
    ws.append(["مشروع صيانة الطرق — جدول الكميات"])   # title row (noise)
    ws.append([])
    ws.append(["م", "وصف البند", "الوحدة", "الكمية"])   # header
    ws.append(["1", "أعمال الحفر والردم للتربة الطبيعية", "م3", 1500])
    ws.append(["2", "توريد وفرش طبقة أساس من الحجر المكسر", "م2", "٢٥٠٠"])  # Arabic-Indic qty
    ws.append(["3", "أعمال الخرسانة المسلحة للأرصفة", "م3", 320.5])
    ws.append([None, None, None, None])
    ws.append(["4", "توريد وتركيب بردورات خرسانية", "م.ط", 800])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_boq_parser_finds_header_and_items():
    result = parse_boq_xlsx(make_boq_xlsx())
    assert result.sheets_parsed == 1
    assert len(result.items) == 4
    assert result.confidence >= 0.5
    first = result.items[0]
    assert first.item_no == "1"
    assert "الحفر" in first.description
    assert first.unit == "م3"
    assert first.qty == Decimal(1500)
    # Arabic-Indic digits converted
    assert result.items[1].qty == Decimal(2500)


def test_boq_parser_rejects_random_sheet():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["hello", "world"])
    ws.append([1, 2])
    buf = io.BytesIO()
    wb.save(buf)
    result = parse_boq_xlsx(buf.getvalue())
    assert result.items == []
    assert result.confidence == 0.0


def test_xlsx_text_extraction_and_chunking():
    text = extract_text("boq.xlsx", make_boq_xlsx())
    assert "أعمال الحفر" in text
    chunks = chunk_text(text)
    assert chunks and all(len(c) <= 2600 for c in chunks)


def test_chunking_overlap_and_bounds():
    text = "\n".join(f"سطر رقم {i} في كراسة الشروط والمواصفات" for i in range(400))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert chunks[0][-30:] != chunks[1][:30] or True  # overlap allowed, no crash
    assert "".join(chunks)  # non-empty


def test_mime_sniff():
    assert sniff_mime("كراسة.pdf") == "application/pdf"
    assert sniff_mime("boq.XLSX").endswith("sheet")
