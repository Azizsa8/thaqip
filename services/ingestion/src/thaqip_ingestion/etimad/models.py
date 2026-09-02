"""Pydantic models for Etimad visitor-API payloads.

Field map captured from a live probe of
GET https://tenders.etimad.sa/Tender/AllSupplierTendersForVisitorAsync
on 2026-09-02 (totalCount was 287,896). Raw payloads are always retained
in tenders.payload — these models validate the fields we normalize, and
tolerate additions (extra="allow").
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EtimadTenderRow(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    tender_id: int = Field(alias="tenderId")
    reference_number: str = Field(alias="referenceNumber")
    tender_name: str = Field(alias="tenderName")
    tender_number: str | None = Field(default=None, alias="tenderNumber")
    agency_name: str | None = Field(default=None, alias="agencyName")
    agency_code: str | None = Field(default=None, alias="agencyCode")
    branch_name: str | None = Field(default=None, alias="branchName")
    tender_id_string: str | None = Field(default=None, alias="tenderIdString")
    tender_status_id: int | None = Field(default=None, alias="tenderStatusId")
    tender_status_name: str | None = Field(default=None, alias="tenderStatusName")
    tender_type_id: int | None = Field(default=None, alias="tenderTypeId")
    tender_type_name: str | None = Field(default=None, alias="tenderTypeName")
    tender_activity_id: int | None = Field(default=None, alias="tenderActivityId")
    tender_activity_name: str | None = Field(default=None, alias="tenderActivityName")
    condetional_booklet_price: float | None = Field(default=None, alias="condetionalBookletPrice")
    financial_fees: float | None = Field(default=None, alias="financialFees")
    buying_cost: float | None = Field(default=None, alias="buyingCost")
    invitation_cost: float | None = Field(default=None, alias="invitationCost")
    submition_date: datetime | None = Field(default=None, alias="submitionDate")
    last_enqueries_date: datetime | None = Field(default=None, alias="lastEnqueriesDate")
    last_offer_presentation_date: datetime | None = Field(default=None, alias="lastOfferPresentationDate")
    offers_opening_date: datetime | None = Field(default=None, alias="offersOpeningDate")
    last_enqueries_date_hijri: str | None = Field(default=None, alias="lastEnqueriesDateHijri")
    last_offer_presentation_date_hijri: str | None = Field(default=None, alias="lastOfferPresentationDateHijri")
    offers_opening_date_hijri: str | None = Field(default=None, alias="offersOpeningDateHijri")
    inside_ksa: bool | None = Field(default=None, alias="insideKSA")
    remaining_days: int | None = Field(default=None, alias="remainingDays")
    remaining_hours: int | None = Field(default=None, alias="remainingHours")


class EtimadListingPage(BaseModel):
    model_config = ConfigDict(extra="allow")

    data: list[EtimadTenderRow]
    totalCount: int
    pageSize: int
    currentPage: int
