"""Awards & offers parsing (ticket B4).

The awarding view component for an awarded tender (status 15) contains two
tables (verified live 2026-09-02):

  "قائمة الموردين المتقدمين"   — all bidders: name, financial offer, technical result
  "قائمة الموردين المرسى عليهم" — awardees: name, financial offer, award value

Awarded tenders are discoverable via the public listing with
`TenderCategory=6` ("تم اعلان الترسية") — 238,547 tenders at time of writing.
"""
from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

NOT_ANNOUNCED_MARKER = "لم يتم اعلان"


def _clean(cell: str) -> str:
    return _WS_RE.sub(" ", html_mod.unescape(_TAG_RE.sub(" ", cell))).strip()


def _num(text: str) -> Decimal | None:
    t = text.replace(",", "").replace("ر.س", "").strip()
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


@dataclass
class Bidder:
    name: str
    offer_value: Decimal | None
    technical_result: str | None = None


@dataclass
class Awardee:
    name: str
    offer_value: Decimal | None
    award_value: Decimal | None


@dataclass
class AwardingResult:
    announced: bool
    bidders: list[Bidder] = field(default_factory=list)
    awardees: list[Awardee] = field(default_factory=list)


def parse_awarding_fragment(raw_html: str) -> AwardingResult:
    text = html_mod.unescape(raw_html)
    if NOT_ANNOUNCED_MARKER in _clean(text):
        return AwardingResult(announced=False)

    body = _SCRIPT_RE.sub("", raw_html)
    result = AwardingResult(announced=True)

    for table in _TABLE_RE.findall(body):
        rows = [[_clean(c) for c in _CELL_RE.findall(r)] for r in _ROW_RE.findall(table)]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        header = " ".join(rows[0])
        is_awardee_table = "قيمة الترسية" in header
        is_bidder_table = "نتائج فحص" in header or "فحص العروض" in header
        for cells in rows[1:]:
            if len(cells) < 2 or not cells[0]:
                continue
            if is_awardee_table:
                result.awardees.append(Awardee(
                    name=cells[0],
                    offer_value=_num(cells[1]) if len(cells) > 1 else None,
                    award_value=_num(cells[2]) if len(cells) > 2 else None,
                ))
            elif is_bidder_table:
                result.bidders.append(Bidder(
                    name=cells[0],
                    offer_value=_num(cells[1]) if len(cells) > 1 else None,
                    technical_result=cells[2] if len(cells) > 2 else None,
                ))
    return result
