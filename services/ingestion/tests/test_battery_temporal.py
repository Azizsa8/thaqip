"""TEMPORAL LEAKAGE & POINT-IN-TIME BATTERY (testing guide section 9).

"Temporal leakage detected in primary offline evaluation" is an automatic NO-GO,
so this file is adversarial by construction.  It does three things:

1. **Static audit.**  Every SQL constant in the p2w package that reads a fact
   table is asserted to carry (a) a strict ``< as_of`` cutoff on a knowability
   timestamp and (b) an exclusion of the subject tender.  The audit is written as
   assertions rather than prose so a future query that drops either predicate
   fails the suite instead of silently leaking.

2. **Differential future-mutation.**  A synthetic timeline (t-3y, t-2y, t-1y and
   t+1y) is driven through a fake asyncpg connection that implements the real
   SQL semantics in Python.  Every prediction is computed twice: once with the
   future tender's award at its nominal value, once with it multiplied by a
   thousand.  The two outputs must be **bit-identical** (``to_dict()`` equality,
   ``generated_at`` excluded because it is a wall clock, not a feature).  If any
   future fact reached the model the two runs diverge.

3. **Self-inclusion.**  The subject tender's own award and its own offers must
   never appear in its own evidence, its own comparable set or its own ratio
   sample — including when the caller supplies an explicit ``as_of`` that is
   *later* than the subject's own award became knowable, which is the case the
   implicit point-in-time gate does not cover.

The fake connection is not a mock that returns canned rows: it stores tender /
award / offer records and filters them with the same predicates the SQL states,
so a query that forgets a predicate genuinely sees rows it should not.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from thaqip_ingestion.p2w import competitor as comp
from thaqip_ingestion.p2w import evidence as ev
from thaqip_ingestion.p2w import market
from thaqip_ingestion.p2w import participation as part
from thaqip_ingestion.p2w import similarity as sim

DAY = timedelta(days=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SUBJECT_ID = 1
ACTIVITY = 111
AGENCY = 222
OTHER_AGENCY = 333
VENDOR = 7
FUTURE_ID = 900
NAME = "صيانة وتشغيل مباني"


# --------------------------------------------------------------------------
# Synthetic timeline + a connection that really applies the SQL predicates.
# --------------------------------------------------------------------------


class Row(dict):
    """asyncpg Records behave like mappings; dict is a faithful enough stand-in."""


class TimelineDB:
    """In-memory tenders/awards/offers that answers the p2w SQL constants.

    Each handler re-implements the WHERE clause of the corresponding SQL by
    reading the predicates the module declares, so the fixture cannot drift into
    being kinder than the database.  Unknown SQL raises: a new query added to the
    package without a handler here fails loudly rather than returning ``[]`` and
    looking clean.
    """

    def __init__(self, tenders: list[dict[str, Any]]) -> None:
        self.tenders = {t["id"]: t for t in tenders}
        self.queries: list[str] = []

    # -- helpers ---------------------------------------------------------
    def _knowable_at(self, t: dict[str, Any]) -> datetime | None:
        """COALESCE(a.awarded_at, a.created_at) — None when there is no award."""
        if t.get("award_value") is None:
            return None
        return t.get("award_awarded_at") or t.get("award_created_at")

    def _awarded(self) -> list[dict[str, Any]]:
        return [t for t in self.tenders.values() if t.get("award_value") is not None]

    # -- dispatch --------------------------------------------------------
    async def fetch(self, sql: str, *args: Any) -> list[Row]:
        self.queries.append(sql)
        # Honour only the predicates the SQL actually declares: deleting a gate
        # from a module must make this fixture leak exactly as postgres would,
        # otherwise the functional tests would pass over a removed WHERE clause.
        self._honour_cutoff = "COALESCE(a.awarded_at, a.created_at) <" in sql
        if sql is ev.MARKET_BY_ACTIVITY_SQL:
            return self._market(*args, key="activity_id", match=args[2])
        if sql is ev.MARKET_BY_AGENCY_SQL:
            return self._market(*args, key="agency_id", match=args[3])
        if sql is ev.COMPETITOR_OFFERS_SQL:
            return self._competitor_offers(*args)
        if sql is sim._CANDIDATE_SQL:
            return self._candidates(*args)
        if sql is comp.VENDOR_RATIO_SQL:
            # The exclusion is honoured only if the SQL really declares it, so
            # deleting the predicate from the module makes this fixture leak
            # exactly as the database would.
            return self._vendor_ratio(*args, honour_exclusion="t.id <> $4" in sql)
        if sql is comp.CATEGORY_RATIO_SQL:
            return self._category_ratio(*args, honour_exclusion="t.id <> $3" in sql)
        if sql is part.CANDIDATE_HISTORY_SQL:
            return self._candidate_history(*args)
        if sql is part.VENDOR_HISTORY_SQL:
            return self._vendor_history(*args)
        raise AssertionError(f"TimelineDB has no handler for this SQL:\n{sql}")

    # -- handlers --------------------------------------------------------
    def _market(self, tender_id, cutoff, activity_id, agency_id, *, key, match):
        out = []
        for t in self._awarded():
            if t["id"] == tender_id:
                continue
            if t.get(key) != match:
                continue
            k = self._knowable_at(t)
            if k is None or (self._honour_cutoff and not k < cutoff):
                continue
            same_agency = True if key == "agency_id" else (
                t.get("agency_id") is not None and t.get("agency_id") == agency_id
            )
            out.append(Row(knowable_at=k, same_agency=same_agency))
        return out

    def _competitor_offers(self, tender_id, cutoff, activity_id, agency_id, vendor_id):
        out = []
        for t in self._awarded():
            if t["id"] == tender_id:
                continue
            k = self._knowable_at(t)
            if k is None or (self._honour_cutoff and not k < cutoff):
                continue
            for v, value in t.get("offers", []):
                if v != vendor_id or value is None:
                    continue
                out.append(
                    Row(
                        knowable_at=k,
                        same_agency=(
                            t.get("agency_id") is not None and t.get("agency_id") == agency_id
                        ),
                        same_activity=(
                            t.get("activity_id") is not None
                            and t.get("activity_id") == activity_id
                        ),
                    )
                )
        return out

    def _candidates(self, tender_id, activity_id, agency_id, limit):
        out = []
        for t in sorted(self.tenders.values(), key=lambda r: r["id"]):
            if t["id"] == tender_id:
                continue
            if not (
                t.get("activity_id") == activity_id
                or t.get("agency_id") == agency_id
                or t.get("award_value") is not None
            ):
                continue
            out.append(
                Row(
                    id=t["id"],
                    name=t.get("name"),
                    agency_id=t.get("agency_id"),
                    agency_name_raw=t.get("agency_name_raw"),
                    branch_name=t.get("branch_name"),
                    activity_id=t.get("activity_id"),
                    activity_name_raw=t.get("activity_name_raw"),
                    booklet_price=t.get("booklet_price"),
                    published_at=t.get("published_at"),
                    last_offer_date=t.get("last_offer_date"),
                    offers_opening_date=t.get("offers_opening_date"),
                    award_value=t.get("award_value"),
                    bidder_count=len(t.get("offers", [])),
                )
            )
        return out[:limit]

    def _vendor_ratio(self, vendor_id, cutoff, activity_id, *rest, honour_exclusion=True):
        exclude = rest[0] if (rest and honour_exclusion) else None
        out = []
        for t in self._awarded():
            if exclude is not None and t["id"] == exclude:
                continue
            k = self._knowable_at(t)
            if k is None or (self._honour_cutoff and not k < cutoff):
                continue
            award = float(t["award_value"])
            if award < comp.MIN_PLAUSIBLE_VALUE:
                continue
            for v, value in t.get("offers", []):
                if v != vendor_id or value is None or value < comp.MIN_PLAUSIBLE_VALUE:
                    continue
                out.append(
                    Row(
                        log_ratio=math.log(float(value) / award),
                        knowable_at=k,
                        same_activity=(
                            t.get("activity_id") is not None
                            and t.get("activity_id") == activity_id
                        ),
                    )
                )
        return out

    def _category_ratio(self, activity_id, cutoff, *rest, honour_exclusion=True):
        exclude = rest[0] if (rest and honour_exclusion) else None
        out = []
        for t in self._awarded():
            if exclude is not None and t["id"] == exclude:
                continue
            k = self._knowable_at(t)
            if k is None or (self._honour_cutoff and not k < cutoff):
                continue
            award = float(t["award_value"])
            if award < comp.MIN_PLAUSIBLE_VALUE:
                continue
            if activity_id is not None and t.get("activity_id") != activity_id:
                continue
            for _v, value in t.get("offers", []):
                if value is None or value < comp.MIN_PLAUSIBLE_VALUE:
                    continue
                out.append(Row(log_ratio=math.log(float(value) / award), knowable_at=k))
        return out

    def _history_rows(self, tender_id, cutoff):
        rows = []
        for t in self._awarded():
            if t["id"] == tender_id:
                continue
            k = self._knowable_at(t)
            if k is None or (self._honour_cutoff and not k < cutoff):
                continue
            for v, _value in t.get("offers", []):
                if v is None:
                    continue
                rows.append(
                    Row(
                        vendor_id=v,
                        tender_id=t["id"],
                        activity_id=t.get("activity_id"),
                        agency_id=t.get("agency_id"),
                        knowable_at=k,
                    )
                )
        return rows

    def _candidate_history(self, tender_id, cutoff, activity_id, agency_id):
        rows = self._history_rows(tender_id, cutoff)
        candidates = {
            r["vendor_id"]
            for r in rows
            if (activity_id is not None and r["activity_id"] == activity_id)
            or (agency_id is not None and r["agency_id"] == agency_id)
        }
        return [r for r in rows if r["vendor_id"] in candidates]

    def _vendor_history(self, tender_id, cutoff, vendor_id):
        return [r for r in self._history_rows(tender_id, cutoff) if r["vendor_id"] == vendor_id]


def _tender(
    tid: int,
    *,
    opening: datetime,
    activity: int | None = ACTIVITY,
    agency: int | None = AGENCY,
    award: float | None = None,
    offers: list[tuple[int, float]] | None = None,
) -> dict[str, Any]:
    return {
        "id": tid,
        "name": NAME,
        "activity_id": activity,
        "activity_name_raw": "صيانة",
        "agency_id": agency,
        "agency_name_raw": "أمانة منطقة الرياض",
        "branch_name": None,
        "booklet_price": 1000.0,
        "published_at": opening - 40 * DAY,
        "last_offer_date": opening - DAY,
        "offers_opening_date": opening,
        "award_value": award,
        # awarded_at is NULL for every row in the real corpus; created_at (the
        # ingestion moment) is the knowability proxy the modules use.
        "award_awarded_at": None,
        "award_created_at": (opening + 30 * DAY) if award is not None else None,
        "offers": offers or [],
    }


#: Ratios that make vendor VENDOR's bids a well-identified 1.08x of the winner.
_RATIOS = [1.05, 1.08, 1.11, 1.07, 1.09, 1.06, 1.12, 1.08, 1.10, 1.04, 1.09, 1.07]


def build_timeline(*, future_award: float = 4_200_000.0, subject_award: float | None = None):
    """Past comparables spanning t-600d..t-48d, a future tender at t+1y, subject at t0."""
    rows: list[dict[str, Any]] = []
    for i in range(24):
        # Spread openings from ~20 months back to ~2 months back.
        opening = T0 - timedelta(days=600 - i * 24)
        award = 4_000_000.0 + i * 25_000.0
        offers: list[tuple[int, float]] = [(50 + i, award)]  # the winner
        if i >= 24 - len(_RATIOS):
            offers.append((VENDOR, round(award * _RATIOS[i - (24 - len(_RATIOS))], 2)))
        rows.append(
            _tender(
                100 + i,
                opening=opening,
                agency=AGENCY if i % 2 == 0 else OTHER_AGENCY,
                award=award,
                offers=offers,
            )
        )
    # The future tender: awarded a year AFTER the subject's decision moment.
    rows.append(
        _tender(
            FUTURE_ID,
            opening=T0 + timedelta(days=365),
            award=future_award,
            offers=[(51, future_award), (VENDOR, future_award * 1.5)],
        )
    )
    subject = _tender(
        SUBJECT_ID,
        opening=T0,
        award=subject_award,
        offers=[(52, subject_award or 0.0), (VENDOR, (subject_award or 0.0) * 1.2)]
        if subject_award is not None
        else [],
    )
    if subject_award is not None:
        # Ingested BEFORE the caller's explicit as_of — the case the implicit
        # point-in-time gate does not cover.
        subject["award_created_at"] = T0 - 10 * DAY
    rows.append(subject)
    return TimelineDB(rows), dict(subject)


def strip_clock(payload: Any) -> Any:
    """Drop wall-clock fields so 'bit-identical' means identical *features*."""
    if isinstance(payload, dict):
        return {k: strip_clock(v) for k, v in payload.items() if k != "generated_at"}
    if isinstance(payload, list):
        return [strip_clock(v) for v in payload]
    return payload


# --------------------------------------------------------------------------
# 1. Static audit of every fact-reading SQL constant.
# --------------------------------------------------------------------------

#: (module.constant name, sql, needs subject exclusion) — the full audited list.
AUDITED_SQL = [
    ("evidence.MARKET_BY_ACTIVITY_SQL", ev.MARKET_BY_ACTIVITY_SQL, True),
    ("evidence.MARKET_BY_AGENCY_SQL", ev.MARKET_BY_AGENCY_SQL, True),
    ("evidence.COMPETITOR_OFFERS_SQL", ev.COMPETITOR_OFFERS_SQL, True),
    ("competitor.VENDOR_RATIO_SQL", comp.VENDOR_RATIO_SQL, True),
    ("competitor.CATEGORY_RATIO_SQL", comp.CATEGORY_RATIO_SQL, True),
    ("participation.CANDIDATE_HISTORY_SQL", part.CANDIDATE_HISTORY_SQL, True),
    ("participation.VENDOR_HISTORY_SQL", part.VENDOR_HISTORY_SQL, True),
]


@pytest.mark.parametrize("name,sql,_needs", AUDITED_SQL, ids=[a[0] for a in AUDITED_SQL])
def test_every_fact_query_gates_on_a_knowability_timestamp(name, sql, _needs):
    """Each query must compare a knowability timestamp strictly against as_of."""
    assert "COALESCE(a.awarded_at, a.created_at) <" in sql, (
        f"{name} does not carry a strict point-in-time cutoff"
    )
    assert ">=" not in sql.split("COALESCE(a.awarded_at, a.created_at) <")[1][:4]


@pytest.mark.parametrize("name,sql,needs", AUDITED_SQL, ids=[a[0] for a in AUDITED_SQL])
def test_every_fact_query_excludes_the_subject_tender(name, sql, needs):
    """A tender must never be evidence for itself.

    The point-in-time cutoff alone is not sufficient: a caller may legitimately
    pass an ``as_of`` later than the subject's own award became knowable (a
    console re-run on a closed tender, a tender with no dates where the cutoff
    falls back to ``now``), and then only an explicit id predicate keeps the
    subject out of its own sample.
    """
    if not needs:
        return
    assert "t.id <> $" in sql, f"{name} does not exclude the subject tender"


def test_similarity_candidate_sql_excludes_self():
    assert "t.id <> $1" in sim._CANDIDATE_SQL


def test_calibration_pool_refuses_the_ingestion_clock_as_a_decision_moment():
    """calibrate() must order history by real tender dates, not by crawl order."""
    assert "COALESCE(t.offers_opening_date, t.last_offer_date) AS cutoff" in (
        part.CALIBRATION_POOL_SQL
    )
    assert "COALESCE(a.awarded_at, a.created_at) AS cutoff" not in part.CALIBRATION_POOL_SQL


# --------------------------------------------------------------------------
# 2. Differential future-mutation: nothing after as_of may move the number.
# --------------------------------------------------------------------------


async def _market(db, subject, as_of=T0):
    return await market.market_quantiles(db, tender=subject, as_of=as_of)


@pytest.mark.asyncio
async def test_market_prediction_is_bit_identical_under_future_award_mutation():
    baseline_db, subject = build_timeline(future_award=4_200_000.0)
    mutated_db, _ = build_timeline(future_award=4_200_000_000.0)

    a = await _market(baseline_db, subject)
    b = await _market(mutated_db, subject)

    assert not a.is_suppressed, "fixture must produce a real prediction to be meaningful"
    assert strip_clock(a.to_dict()) == strip_clock(b.to_dict())


@pytest.mark.asyncio
async def test_market_curve_is_bit_identical_under_future_award_mutation():
    baseline_db, subject = build_timeline()
    mutated_db, _ = build_timeline(future_award=4_200_000_000.0)
    grid = [3_500_000.0, 4_000_000.0, 4_500_000.0]

    a = await market.market_curve(baseline_db, tender=subject, as_of=T0, grid=grid)
    b = await market.market_curve(mutated_db, tender=subject, as_of=T0, grid=grid)

    assert a and all(p["sample_size"] > 0 for p in a)
    assert a == b


@pytest.mark.asyncio
async def test_similarity_is_bit_identical_under_future_award_mutation():
    baseline_db, subject = build_timeline()
    mutated_db, _ = build_timeline(future_award=4_200_000_000.0)

    a = await sim.find_similar_tenders(baseline_db, tender=subject, as_of=T0, limit=50)
    b = await sim.find_similar_tenders(mutated_db, tender=subject, as_of=T0, limit=50)

    assert a, "retrieval must return comparables for this assertion to bite"
    assert [x.to_dict() for x in a] == [x.to_dict() for x in b]


@pytest.mark.asyncio
async def test_similarity_marks_the_future_tender_excluded_not_merely_unranked():
    db, subject = build_timeline()
    everything = await sim.find_similar_tenders(
        db, tender=subject, as_of=T0, limit=100, include_excluded=True
    )
    future = [x for x in everything if x.tender_id == FUTURE_ID]
    assert future, "the future tender must be visible in the audit trail"
    assert future[0].exclusion_reason == sim.EXCLUSION_FUTURE
    eligible = await sim.find_similar_tenders(db, tender=subject, as_of=T0, limit=100)
    assert FUTURE_ID not in {x.tender_id for x in eligible}


@pytest.mark.asyncio
async def test_market_evidence_is_bit_identical_under_future_award_mutation():
    baseline_db, subject = build_timeline()
    mutated_db, _ = build_timeline(future_award=4_200_000_000.0)
    a = await ev.market_evidence(baseline_db, tender=subject, as_of=T0)
    b = await ev.market_evidence(mutated_db, tender=subject, as_of=T0)
    assert a.comparable_count > 0
    assert a.to_dict() == b.to_dict()


@pytest.mark.asyncio
async def test_competitor_prediction_is_bit_identical_under_future_award_mutation():
    baseline_db, subject = build_timeline()
    mutated_db, _ = build_timeline(future_award=4_200_000_000.0)

    a = await comp.competitor_quantiles(baseline_db, vendor_id=VENDOR, tender=subject, as_of=T0)
    b = await comp.competitor_quantiles(mutated_db, vendor_id=VENDOR, tender=subject, as_of=T0)

    assert not a.is_suppressed, "fixture must reach a real competitor prediction"
    assert strip_clock(a.to_dict()) == strip_clock(b.to_dict())


@pytest.mark.asyncio
async def test_participation_is_bit_identical_under_future_data_mutation():
    baseline_db, subject = build_timeline()
    mutated_db, _ = build_timeline(future_award=4_200_000_000.0)

    a = await part.candidate_bidders(baseline_db, tender=subject, as_of=T0, limit=20)
    b = await part.candidate_bidders(mutated_db, tender=subject, as_of=T0, limit=20)

    assert a, "fixture must produce candidate bidders"
    assert [x.to_dict() for x in a] == [x.to_dict() for x in b]


@pytest.mark.asyncio
async def test_participation_does_not_count_the_future_tender_as_history():
    db, subject = build_timeline()
    est = await part.participation_probability(db, vendor_id=VENDOR, tender=subject, as_of=T0)
    # VENDOR bids on 12 past tenders and on the future one; only 12 are knowable.
    assert est.features is not None
    assert est.features.total_count == len(_RATIOS)


# --------------------------------------------------------------------------
# 3. feature_snapshot_id: stable under future mutation, moves under past.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_snapshot_id_is_stable_under_future_data_mutation():
    baseline_db, subject = build_timeline()
    mutated_db, _ = build_timeline(future_award=4_200_000_000.0)
    a = await _market(baseline_db, subject)
    b = await _market(mutated_db, subject)
    assert a.feature_snapshot_id == b.feature_snapshot_id


@pytest.mark.asyncio
async def test_feature_snapshot_id_changes_when_a_past_comparable_is_removed():
    baseline_db, subject = build_timeline()
    a = await _market(baseline_db, subject)

    dropped_db, _ = build_timeline()
    del dropped_db.tenders[105]
    c = await _market(dropped_db, subject)

    assert c.feature_snapshot_id != a.feature_snapshot_id, (
        "the snapshot id must identify the contributing sample, not just the date"
    )


@pytest.mark.asyncio
async def test_competitor_snapshot_id_is_stable_under_future_data_mutation():
    baseline_db, subject = build_timeline()
    mutated_db, _ = build_timeline(future_award=4_200_000_000.0)
    a = await comp.competitor_quantiles(baseline_db, vendor_id=VENDOR, tender=subject, as_of=T0)
    b = await comp.competitor_quantiles(mutated_db, vendor_id=VENDOR, tender=subject, as_of=T0)
    assert a.feature_snapshot_id == b.feature_snapshot_id


def test_market_snapshot_id_is_a_pure_function_of_sample_asof_and_version():
    ids = [3, 1, 2]
    assert market.feature_snapshot_id(ids, T0) == market.feature_snapshot_id([1, 2, 3], T0)
    assert market.feature_snapshot_id(ids, T0) != market.feature_snapshot_id(ids, T0 + DAY)
    assert market.feature_snapshot_id(ids, T0) != market.feature_snapshot_id(
        ids, T0, model_version="p2w-9.9.9"
    )
    # Same day, different hour: the snapshot must not churn within a day.
    assert market.feature_snapshot_id(ids, T0) == market.feature_snapshot_id(
        ids, T0 + timedelta(hours=13)
    )


# --------------------------------------------------------------------------
# 4. Self-inclusion: the subject is never its own evidence.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subject_award_is_not_in_its_own_comparable_set():
    """Even with an award already on record and an as_of after it was ingested."""
    db, subject = build_timeline(subject_award=9_999_999.0)
    late = T0 + 5 * DAY  # explicit cutoff AFTER the subject's award became knowable

    everything = await sim.find_similar_tenders(
        db, tender=subject, as_of=late, limit=100, include_excluded=True
    )
    assert SUBJECT_ID not in {x.tender_id for x in everything}

    evidence = await ev.market_evidence(db, tender=subject, as_of=late)
    clean_db, _ = build_timeline()
    clean = await ev.market_evidence(clean_db, tender=subject, as_of=late)
    assert evidence.to_dict() == clean.to_dict()


@pytest.mark.asyncio
async def test_subject_own_offers_are_not_in_its_own_competitor_ratio_sample():
    """The subject's own bid/award pair must never train the model that predicts it.

    ``competitor_ratio_sample`` documents the exclusion as implicit ("its own
    award is not knowable before its own cutoff").  That holds only while the
    cutoff is derived from the subject's own dates.  Here the caller passes an
    explicit ``as_of`` five days after the award was ingested — exactly what a
    re-run on a closed tender does — and the implicit gate stops covering it.
    """
    leaky_db, _subject = build_timeline(subject_award=9_999_999.0)
    clean_db, _clean_subject = build_timeline()
    late = T0 + 5 * DAY

    leaky = await comp.competitor_ratio_sample(
        leaky_db, vendor_id=VENDOR, activity_id=ACTIVITY, as_of=late, tender_id=SUBJECT_ID
    )
    clean = await comp.competitor_ratio_sample(
        clean_db, vendor_id=VENDOR, activity_id=ACTIVITY, as_of=late, tender_id=SUBJECT_ID
    )
    assert leaky == clean


@pytest.mark.asyncio
async def test_subject_own_offers_are_not_in_its_own_category_prior():
    leaky_db, _subject = build_timeline(subject_award=9_999_999.0)
    clean_db, _ = build_timeline()
    late = T0 + 5 * DAY

    leaky = await comp.category_prior_sample(
        leaky_db, activity_id=ACTIVITY, as_of=late, tender_id=SUBJECT_ID
    )
    clean = await comp.category_prior_sample(
        clean_db, activity_id=ACTIVITY, as_of=late, tender_id=SUBJECT_ID
    )
    assert leaky == clean


@pytest.mark.asyncio
async def test_competitor_quantiles_ignores_the_subjects_own_award_end_to_end():
    """The full competitor path, driven with an explicit post-award cutoff."""
    leaky_db, subject = build_timeline(subject_award=9_999_999.0)
    clean_db, clean_subject = build_timeline()
    late = T0 + 5 * DAY

    leaky = await comp.competitor_quantiles(
        leaky_db, vendor_id=VENDOR, tender=subject, as_of=late
    )
    clean = await comp.competitor_quantiles(
        clean_db, vendor_id=VENDOR, tender=clean_subject, as_of=late
    )
    assert not clean.is_suppressed
    assert strip_clock(leaky.to_dict()) == strip_clock(clean.to_dict())


@pytest.mark.asyncio
async def test_participation_ignores_the_subjects_own_offers():
    leaky_db, subject = build_timeline(subject_award=9_999_999.0)
    clean_db, clean_subject = build_timeline()
    late = T0 + 5 * DAY
    leaky = await part.participation_probability(
        leaky_db, vendor_id=VENDOR, tender=subject, as_of=late
    )
    clean = await part.participation_probability(
        clean_db, vendor_id=VENDOR, tender=clean_subject, as_of=late
    )
    assert leaky.to_dict() == clean.to_dict()


@pytest.mark.asyncio
async def test_subject_own_award_value_is_not_a_similarity_input():
    """A post-close fact on the subject row must not change how it is scored.

    ``score_candidate`` compares magnitudes via ``_scale_pair``, which prefers
    award value on *both* sides.  For a pre-close prediction the subject has no
    award, but nothing stops a caller (a backtest harness, a joined query) from
    handing over a row that carries one, and then the outcome being predicted
    quietly becomes an input to retrieval.
    """
    db, _ = build_timeline()
    plain = dict(build_timeline()[1])
    with_award = dict(plain)
    with_award["award_value"] = 40_000_000.0  # 10x the market

    a = await sim.find_similar_tenders(db, tender=plain, as_of=T0, limit=50)
    b = await sim.find_similar_tenders(db, tender=with_award, as_of=T0, limit=50)
    assert [x.to_dict() for x in a] == [x.to_dict() for x in b]


# --------------------------------------------------------------------------
# 5. Cutoff resolution: ambiguity must resolve strictly, never loosely.
# --------------------------------------------------------------------------


def test_market_cutoff_takes_the_earlier_of_the_two_horizons():
    """Ambiguity resolves strictly: the earlier of the two derived horizons.

    ``evidence.resolve_as_of`` stops at ``last_offer_date`` and then falls back
    to *now*; ``similarity.point_in_time`` also accepts ``published_at``.  When
    only a publication date exists, the loose horizon is "now" and the strict one
    is the publication date — the strict one must win, or a tender with a missing
    closing date would silently be graded against the whole present corpus.
    """
    dated = {"id": SUBJECT_ID, "offers_opening_date": T0, "last_offer_date": T0 - DAY}
    assert market._resolve_cutoff(dated, None) == T0

    published_only = {"id": SUBJECT_ID, "published_at": T0 - 40 * DAY}
    assert market._resolve_cutoff(published_only, None) == T0 - 40 * DAY
    # ...and that is strictly earlier than what evidence.resolve_as_of alone gives.
    assert ev.resolve_as_of(published_only, None) > T0 - 40 * DAY

    # An explicit as_of wins outright — the caller is stating the horizon.
    assert market._resolve_cutoff(dated, T0 - 400 * DAY) == T0 - 400 * DAY


def test_similarity_excludes_a_candidate_with_no_provable_timestamp():
    subject = {"id": 1, "name": NAME, "activity_id": ACTIVITY, "agency_id": AGENCY}
    undated = {"id": 2, "name": NAME, "activity_id": ACTIVITY, "award_value": 1.0}
    scored = sim.score_candidate(subject, undated, as_of=T0)
    assert scored.exclusion_reason == sim.EXCLUSION_FUTURE
    assert "no_point_in_time_timestamp" in scored.notes


def test_candidate_exactly_at_the_cutoff_is_excluded():
    """The gate is strict ``<``: a tender opening at as_of is not yet knowable."""
    subject = {"id": 1, "name": NAME, "activity_id": ACTIVITY, "agency_id": AGENCY}
    boundary = {
        "id": 2,
        "name": NAME,
        "activity_id": ACTIVITY,
        "offers_opening_date": T0,
        "award_value": 1_000_000.0,
    }
    assert sim.score_candidate(subject, boundary, as_of=T0).exclusion_reason == (
        sim.EXCLUSION_FUTURE
    )
    just_before = dict(boundary, offers_opening_date=T0 - timedelta(seconds=1))
    assert sim.score_candidate(subject, just_before, as_of=T0).exclusion_reason is None


@pytest.mark.asyncio
async def test_moving_as_of_backwards_can_only_shrink_the_evidence():
    """Monotonicity: an earlier horizon must never see more facts than a later one."""
    db, subject = build_timeline()
    counts = []
    for offset in (0, 365, 730):
        e = await ev.market_evidence(db, tender=subject, as_of=T0 - timedelta(days=offset))
        counts.append(e.comparable_count)
    assert counts == sorted(counts, reverse=True), counts
    assert counts[0] > counts[-1], "the fixture must actually span the horizons"


@pytest.mark.asyncio
async def test_adding_whole_future_tenders_changes_nothing():
    """Value mutation cannot move a *count*; adding future rows can.

    A gate that filtered on value plausibility rather than on time would pass the
    mutation tests above, so this one grows the corpus after ``as_of`` instead:
    thirty extra awarded tenders, all opened a year later, must be invisible to
    every model at ``as_of``.
    """
    baseline_db, subject = build_timeline()
    grown_db, _ = build_timeline()
    for n in range(30):
        extra = _tender(
            5000 + n,
            opening=T0 + timedelta(days=365 + n),
            award=7_000_000.0 + n,
            offers=[(60 + n, 7_000_000.0 + n), (VENDOR, 8_000_000.0 + n)],
        )
        grown_db.tenders[extra["id"]] = extra

    base_ev = await ev.market_evidence(baseline_db, tender=subject, as_of=T0)
    grown_ev = await ev.market_evidence(grown_db, tender=subject, as_of=T0)
    assert base_ev.to_dict() == grown_ev.to_dict()

    base_ce = await ev.competitor_evidence(baseline_db, vendor_id=VENDOR, tender=subject, as_of=T0)
    grown_ce = await ev.competitor_evidence(grown_db, vendor_id=VENDOR, tender=subject, as_of=T0)
    assert base_ce.to_dict() == grown_ce.to_dict()

    base_m = await _market(baseline_db, subject)
    grown_m = await _market(grown_db, subject)
    assert strip_clock(base_m.to_dict()) == strip_clock(grown_m.to_dict())

    base_c = await comp.competitor_quantiles(
        baseline_db, vendor_id=VENDOR, tender=subject, as_of=T0
    )
    grown_c = await comp.competitor_quantiles(grown_db, vendor_id=VENDOR, tender=subject, as_of=T0)
    assert strip_clock(base_c.to_dict()) == strip_clock(grown_c.to_dict())

    base_p = await part.candidate_bidders(baseline_db, tender=subject, as_of=T0, limit=50)
    grown_p = await part.candidate_bidders(grown_db, tender=subject, as_of=T0, limit=50)
    assert [x.to_dict() for x in base_p] == [x.to_dict() for x in grown_p]
