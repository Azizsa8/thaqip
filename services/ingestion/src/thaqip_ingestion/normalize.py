"""Normalize Etimad rows into canonical tender upserts (ticket C2).

Idempotency contract: replaying the same source row twice must emit zero
events. We hash the normalized payload; an upsert only fires an event when
the hash changes, and the event carries the changed field names.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .etimad.models import EtimadTenderRow

# Fields whose changes are meaningful enough to notify users about (M2-3).
EVENTFUL_FIELDS = {
    "status_id": "tender.updated",
    "last_offer_date": "tender.extended",
    "last_enquiries_date": "tender.updated",
    "offers_opening_date": "tender.updated",
    "name": "tender.updated",
}


def to_canonical(row: EtimadTenderRow) -> dict[str, Any]:
    return {
        "source": "etimad",
        "source_tender_id": row.tender_id,
        "source_id_string": row.tender_id_string,
        "reference_number": row.reference_number,
        "tender_number": row.tender_number,
        "name": row.tender_name,
        "agency_name_raw": row.agency_name,
        "branch_name": row.branch_name,
        "activity_id": row.tender_activity_id,
        "activity_name_raw": row.tender_activity_name,
        "tender_type_id": row.tender_type_id,
        "tender_type_name": row.tender_type_name,
        "status_id": row.tender_status_id,
        "status_name": row.tender_status_name,
        "booklet_price": row.condetional_booklet_price,
        "financial_fees": row.financial_fees,
        "buying_cost": row.buying_cost,
        "invitation_cost": row.invitation_cost,
        "submission_date": _iso(row.submition_date),
        "last_enquiries_date": _iso(row.last_enqueries_date),
        "last_offer_date": _iso(row.last_offer_presentation_date),
        "offers_opening_date": _iso(row.offers_opening_date),
        "last_enquiries_date_hijri": row.last_enqueries_date_hijri,
        "last_offer_date_hijri": row.last_offer_presentation_date_hijri,
        "offers_opening_date_hijri": row.offers_opening_date_hijri,
        "inside_ksa": row.inside_ksa,
        "published_at": _iso(row.submition_date),
    }


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def content_hash(canonical: dict[str, Any]) -> str:
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def diff_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    return sorted(k for k in new if old.get(k) != new.get(k))


def classify_change(changed: list[str]) -> str:
    """Pick the most specific event type for a set of changed fields."""
    if "last_offer_date" in changed:
        return "tender.extended"
    if "status_id" in changed:
        return "tender.updated"  # refined to awarded/cancelled once status map (B2) lands
    return "tender.updated"
