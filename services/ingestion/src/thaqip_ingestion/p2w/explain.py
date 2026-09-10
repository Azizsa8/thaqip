"""Explainability service (FR-012): why a P2W number says what it says.

The product rule this module exists to enforce is the first house rule: an
**observed fact**, a **derived aggregate** and a **model prediction** are three
different kinds of claim and must never be rendered alike. So they are three
different lists in the output, built by three different code paths, and the
``observed`` list is guarded:

  * every entry names the table and row id it came from (``source_table`` /
    ``source_id``), so a user can be shown the record behind the number;
  * where a ``source_lineage`` row exists for that fact, the entry also carries
    the ``source_id`` of the registered source and the ``parser_version`` that
    produced it;
  * an entry that cannot be traced to a row is **dropped**, not shown, and the
    drop is reported in ``data_quality_warnings``. A fact we cannot point at is
    not an observation, it is a rumour.

``_assert_observed_are_facts`` re-checks this structurally before returning, so
a future edit that puts a model output into ``observed`` fails loudly rather
than silently telling a user that a prediction is a fact.

Lineage note, honestly stated: ``source_lineage`` is currently **empty** in this
deployment — the ingestion path does not yet write it. Every observed entry
therefore carries ``lineage: None`` and the explanation says so in a data
quality warning, rather than implying a provenance chain that does not exist.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .contracts import (
    MODEL_VERSION,
    ExplanationFactor,
    PricePrediction,
)

log = logging.getLogger(__name__)

#: Tables an ``observed`` entry is permitted to cite. An observation must come
#: from a record of something that actually happened, never from a model table.
OBSERVED_TABLES: frozenset[str] = frozenset({"tenders", "awards", "offers", "vendors", "agencies"})

#: Keys every ``observed`` entry carries. Kept as a constant so the guard and
#: the builders cannot drift apart.
OBSERVED_KEYS: tuple[str, ...] = (
    "label",
    "value",
    "unit",
    "tender_id",
    "evidence_ref",
    "as_of",
    "kind",
    "source_table",
    "source_id",
    "lineage",
)

#: Below this many usable comparables the sample is called out as thin.
THIN_SAMPLE_THRESHOLD = 5

#: Above this median comparable age (days) the sample is called out as ageing.
AGEING_SAMPLE_DAYS = 365.0

UNIT_SAR = "SAR"
UNIT_NAME = "name"

_LINEAGE_SQL = """
SELECT DISTINCT ON (l.fact_table, l.fact_id)
       l.fact_table,
       l.fact_id,
       l.source_id,
       l.source_object_ref,
       l.content_hash,
       l.parser_version,
       l.access_class,
       l.retrieved_at,
       r.name          AS source_name,
       r.access_class  AS registry_access_class
FROM source_lineage l
LEFT JOIN source_registry r ON r.source_id = l.source_id
WHERE (l.fact_table, l.fact_id) IN (
    SELECT * FROM unnest($1::text[], $2::bigint[])
)
ORDER BY l.fact_table, l.fact_id, l.recorded_at DESC
"""

_SINGLE_LINEAGE_SQL = """
SELECT l.fact_table,
       l.fact_id,
       l.source_id,
       l.source_object_ref,
       l.content_hash,
       l.parser_version,
       l.access_class,
       l.retrieved_at,
       r.name          AS source_name,
       r.access_class  AS registry_access_class
FROM source_lineage l
LEFT JOIN source_registry r ON r.source_id = l.source_id
WHERE l.fact_table = $1 AND l.fact_id = $2
ORDER BY l.recorded_at DESC
LIMIT 1
"""

#: One award per comparable tender, plus the dates that make it citable.
_COMPARABLE_FACTS_SQL = """
SELECT t.id                  AS tender_id,
       t.name                AS tender_name,
       t.published_at,
       t.last_offer_date,
       t.offers_opening_date,
       a.id                  AS award_id,
       a.award_value,
       a.awarded_at
FROM tenders t
LEFT JOIN LATERAL (
    SELECT aw.id, aw.award_value, aw.awarded_at
    FROM awards aw
    WHERE aw.tender_id = t.id AND aw.award_value IS NOT NULL
    ORDER BY aw.id
    LIMIT 1
) a ON TRUE
WHERE t.id = ANY($1::bigint[])
ORDER BY t.id
"""

_VENDOR_FACTS_SQL = """
SELECT v.id, v.canonical_name, v.cr_number
FROM vendors v
WHERE v.id = ANY($1::bigint[])
ORDER BY v.id
"""


# --------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------


def _field(item: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a dataclass-like object or a mapping, indifferently."""
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _as_float(value: Any) -> float | None:
    """Best-effort float, or None. Decimals from asyncpg land here."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _median(values: Sequence[float]) -> float | None:
    """Plain median (pure python — numpy is not installed and is not wanted)."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _point_in_time(row: Mapping[str, Any]) -> str | None:
    """When the comparable's price became knowable, as an ISO string.

    Same precedence as ``similarity.point_in_time`` — offers opening, then last
    offer date, then publication — because an observed fact shown next to a date
    must be shown next to *that* date, not the row's insertion timestamp.
    """
    for key in ("offers_opening_date", "last_offer_date", "published_at"):
        stamp = _iso(row.get(key))
        if stamp is not None:
            return stamp
    return None


def _lineage_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a ``source_lineage`` row for display. Keys are stable."""
    return {
        "source_id": row.get("source_id"),
        "source_name": row.get("source_name"),
        "parser_version": row.get("parser_version"),
        "access_class": row.get("access_class") or row.get("registry_access_class"),
        "source_object_ref": row.get("source_object_ref"),
        "content_hash": row.get("content_hash"),
        "retrieved_at": _iso(row.get("retrieved_at")),
    }


# --------------------------------------------------------------------------
# lineage lookup
# --------------------------------------------------------------------------


async def trace_fact(conn: Any, fact_table: str, fact_id: int) -> dict[str, Any]:
    """Provenance for one observed fact: which source produced it, and how.

    Always returns a dict — never raises and never invents. ``lineage`` is None
    when no ``source_lineage`` row exists (the current state of this database for
    every fact), and ``lineage_available`` says which of the two it is so a
    caller cannot mistake "not recorded" for "not applicable".

    ``fact_table`` is validated against :data:`OBSERVED_TABLES`: this function is
    for tracing observations, and asking it to trace ``price_predictions`` is a
    category error worth refusing.
    """
    if fact_table not in OBSERVED_TABLES:
        raise ValueError(
            f"fact_table must be one of {sorted(OBSERVED_TABLES)}, got {fact_table!r}"
        )
    identifier = _as_int(fact_id)
    if identifier is None:
        raise ValueError(f"fact_id must be an integer, got {fact_id!r}")

    row: Any = None
    try:
        row = await conn.fetchrow(_SINGLE_LINEAGE_SQL, fact_table, identifier)
    except Exception as exc:  # noqa: BLE001 - a provenance lookup may never break a page
        log.warning("lineage lookup failed for %s:%s: %s", fact_table, identifier, exc)
        return {
            "fact_table": fact_table,
            "fact_id": identifier,
            "lineage": None,
            "lineage_available": False,
            "note": "lineage lookup failed",
        }

    if row is None:
        return {
            "fact_table": fact_table,
            "fact_id": identifier,
            "lineage": None,
            "lineage_available": False,
            "note": "no source_lineage row recorded for this fact",
        }
    return {
        "fact_table": fact_table,
        "fact_id": identifier,
        "lineage": _lineage_dict(dict(row)),
        "lineage_available": True,
    }


async def _lineage_map(
    conn: Any, refs: Sequence[tuple[str, int]]
) -> dict[tuple[str, int], dict[str, Any]]:
    """Batched :func:`trace_fact` — one query for every fact on the page.

    Returns only the refs that actually have lineage; a missing key means "not
    recorded", which the caller renders as ``lineage: None``.
    """
    if not refs:
        return {}
    tables = [ref[0] for ref in refs]
    ids = [ref[1] for ref in refs]
    try:
        rows = await conn.fetch(_LINEAGE_SQL, tables, ids)
    except Exception as exc:  # noqa: BLE001 - degrade to 'not recorded', never raise
        log.warning("batched lineage lookup failed: %s", exc)
        return {}
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in rows or []:
        row = dict(raw)
        key = (str(row.get("fact_table")), _as_int(row.get("fact_id")) or 0)
        out[key] = _lineage_dict(row)
    return out


# --------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------


def _assert_observed_are_facts(observed: Sequence[Mapping[str, Any]]) -> None:
    """Structural proof that nothing model-generated sits in ``observed``.

    Raises ValueError rather than logging: shipping a prediction dressed as an
    observed fact is the single worst thing this system can do to a user, so it
    must be a crash in development, not a warning nobody reads.
    """
    for index, entry in enumerate(observed):
        where = f"observed[{index}]"
        table = entry.get("source_table")
        if table not in OBSERVED_TABLES:
            raise ValueError(
                f"{where} cites source_table={table!r}, which is not an observed-fact "
                f"table {sorted(OBSERVED_TABLES)}"
            )
        if _as_int(entry.get("source_id")) is None:
            raise ValueError(f"{where} has no traceable source_id")
        if entry.get("kind") != "observed":
            raise ValueError(f"{where} must be kind='observed', got {entry.get('kind')!r}")
        forbidden = {"p10", "p50", "p90", "confidence", "win_probability", "model_version"}
        leaked = forbidden.intersection(entry.keys())
        if leaked:
            raise ValueError(
                f"{where} carries model-output keys {sorted(leaked)}; predictions belong "
                "in the 'predicted' list"
            )


# --------------------------------------------------------------------------
# section builders
# --------------------------------------------------------------------------


def _normalise_competitors(competitors: Sequence[Any]) -> list[dict[str, Any]]:
    """Accept either orchestrator competitor dicts or bare ``PricePrediction``s.

    The orchestrator hands over ``{vendor_id, name, participation, prediction,
    undercut_risk}``; a caller experimenting from a shell is likely to hand over
    the predictions alone. Both are useful and neither should be a TypeError.
    """
    out: list[dict[str, Any]] = []
    for item in competitors or []:
        if isinstance(item, PricePrediction):
            out.append(
                {
                    "vendor_id": item.subject_id,
                    "name": None,
                    "prediction": item,
                    "participation": None,
                    "undercut_risk": None,
                }
            )
            continue
        if isinstance(item, Mapping):
            prediction = item.get("prediction")
            out.append(
                {
                    "vendor_id": item.get("vendor_id"),
                    "name": item.get("name"),
                    "prediction": prediction,
                    "participation": item.get("participation"),
                    "undercut_risk": item.get("undercut_risk"),
                }
            )
    return out


def _prediction_dict(value: Any) -> dict[str, Any] | None:
    """A prediction as a plain dict, whether it arrived as an object or JSON."""
    if value is None:
        return None
    if isinstance(value, PricePrediction):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    return None


async def _observed_comparables(
    conn: Any, similar: Sequence[Any]
) -> tuple[list[dict[str, Any]], int, list[float], list[float]]:
    """Observed award facts for the comparables, each tied to its ``awards`` row.

    Returns ``(entries, untraceable_count, observed_values, ages_days)``. A
    comparable whose award row cannot be found in the database is counted in
    ``untraceable_count`` and left out of ``entries`` — the traceability rule has
    no exception for "we are fairly sure".
    """
    usable = [
        item
        for item in (similar or [])
        if _field(item, "exclusion_reason") is None
        and _as_float(_field(item, "award_value")) is not None
    ]
    if not usable:
        return [], 0, [], []

    ids = sorted({_as_int(_field(item, "tender_id")) for item in usable} - {None})
    try:
        rows = await conn.fetch(_COMPARABLE_FACTS_SQL, ids)
    except Exception as exc:  # noqa: BLE001 - degrade to 'not recorded', never raise
        log.warning("comparable fact lookup failed: %s", exc)
        rows = []
    by_tender = {_as_int(dict(r).get("tender_id")): dict(r) for r in rows or []}

    lineage = await _lineage_map(
        conn,
        [
            ("awards", _as_int(row.get("award_id")))
            for row in by_tender.values()
            if _as_int(row.get("award_id")) is not None
        ],
    )

    entries: list[dict[str, Any]] = []
    values: list[float] = []
    ages: list[float] = []
    untraceable = 0
    for item in usable:
        tender_id = _as_int(_field(item, "tender_id"))
        row = by_tender.get(tender_id)
        award_id = _as_int(row.get("award_id")) if row else None
        award_value = _as_float(row.get("award_value")) if row else None
        if row is None or award_id is None or award_value is None:
            untraceable += 1
            continue
        age = _as_float(_field(item, "age_days"))
        if age is not None:
            ages.append(age)
        values.append(award_value)
        entries.append(
            {
                "label": str(row.get("tender_name") or _field(item, "name") or ""),
                "value": award_value,
                "unit": UNIT_SAR,
                "tender_id": tender_id,
                "evidence_ref": f"awards:{award_id}",
                "as_of": _iso(row.get("awarded_at")) or _point_in_time(row),
                "kind": "observed",
                "source_table": "awards",
                "source_id": award_id,
                "lineage": lineage.get(("awards", award_id)),
            }
        )
    entries.sort(key=lambda e: e["tender_id"] or 0)
    return entries, untraceable, values, ages


async def _observed_vendors(
    conn: Any, competitors: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Observed vendor identities — the one competitor fact that *is* a fact.

    A vendor's registered name is on record. Its likely price is not, and lives
    in ``predicted``; its bid count is an aggregate and lives in ``derived``.
    """
    ids = sorted({_as_int(c.get("vendor_id")) for c in competitors} - {None})
    if not ids:
        return []
    try:
        rows = await conn.fetch(_VENDOR_FACTS_SQL, ids)
    except Exception as exc:  # noqa: BLE001 - degrade to 'not recorded', never raise
        log.warning("vendor fact lookup failed: %s", exc)
        return []
    rows = [dict(r) for r in rows or []]
    lineage = await _lineage_map(
        conn, [("vendors", _as_int(r.get("id")) or 0) for r in rows]
    )
    return [
        {
            "label": str(row.get("canonical_name") or ""),
            "value": str(row.get("canonical_name") or ""),
            "unit": UNIT_NAME,
            "tender_id": None,
            "evidence_ref": f"vendors:{_as_int(row.get('id'))}",
            "as_of": None,
            "kind": "observed",
            "source_table": "vendors",
            "source_id": _as_int(row.get("id")),
            "lineage": lineage.get(("vendors", _as_int(row.get("id")) or 0)),
        }
        for row in rows
        if _as_int(row.get("id")) is not None
    ]


def _derived_entries(
    *,
    prediction: PricePrediction,
    similar: Sequence[Any],
    competitors: Sequence[Mapping[str, Any]],
    observed_values: Sequence[float],
    ages_days: Sequence[float],
) -> list[dict[str, Any]]:
    """Aggregates we computed. Each names its method — that is what makes it
    derived rather than observed."""
    excluded = sum(1 for item in (similar or []) if _field(item, "exclusion_reason") is not None)
    priced = sum(
        1
        for c in competitors
        if isinstance(c.get("prediction"), PricePrediction)
        and not c["prediction"].is_suppressed
    )
    entries: list[dict[str, Any]] = [
        {
            "label": "عدد الصفقات المقارنة المستخدمة",
            "value": len(observed_values),
            "method": "count of retrieved comparables with a traceable award value",
        },
        {
            "label": "عدد الصفقات المستبعدة من المقارنة",
            "value": excluded,
            "method": "count of retrieval candidates carrying an exclusion_reason",
        },
    ]
    median_value = _median(observed_values)
    if median_value is not None:
        entries.append(
            {
                "label": "الوسيط الملاحظ لقيم الترسية المقارنة",
                "value": median_value,
                "method": "median of the observed (unadjusted) comparable award values",
            }
        )
    median_age = _median(ages_days)
    if median_age is not None:
        entries.append(
            {
                "label": "متوسط عمر الأدلة (يوم)",
                "value": median_age,
                "method": "median age in days of the contributing comparables at as_of",
            }
        )
    entries.append(
        {
            "label": "درجة حداثة البيانات",
            "value": prediction.data_freshness_score,
            "method": "linear decay of median evidence age over a 730-day horizon",
        }
    )
    entries.append(
        {
            "label": "درجة تشابه الصفقات",
            "value": prediction.similarity_confidence,
            "method": "structural likeness of the comparable set (activity + agency share)",
        }
    )
    entries.append(
        {
            "label": "مستوى الأدلة",
            "value": prediction.evidence_tier.value if prediction.evidence_tier else None,
            "method": "evidence.classify_tier over comparables, effective n, similarity, freshness",
        }
    )
    if competitors:
        entries.append(
            {
                "label": "منافسون بنطاق سعري مدعوم",
                "value": priced,
                "method": "count of candidate vendors whose evidence tier permits a price band",
            }
        )
    return entries


def _predicted_entries(
    *, prediction: PricePrediction, competitors: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Model outputs, and only model outputs. Suppressed ones are omitted here
    and reported through ``suppression`` / ``data_quality_warnings`` instead —
    a suppressed prediction has no numbers to place in this list."""
    entries: list[dict[str, Any]] = []
    if not prediction.is_suppressed:
        entries.append(
            {
                "label": "النطاق السعري المتوقع",
                "p10": prediction.p10,
                "p50": prediction.p50,
                "p90": prediction.p90,
                "confidence": prediction.confidence_score,
                "model_version": prediction.model_version,
                "scope": prediction.prediction_scope.value,
                "subject_id": prediction.subject_id,
            }
        )
    for competitor in competitors:
        item = competitor.get("prediction")
        if not isinstance(item, PricePrediction) or item.is_suppressed:
            continue
        name = competitor.get("name") or f"vendor:{competitor.get('vendor_id')}"
        entries.append(
            {
                "label": f"النطاق المتوقع لعرض {name}",
                "p10": item.p10,
                "p50": item.p50,
                "p90": item.p90,
                "confidence": item.confidence_score,
                "model_version": item.model_version,
                "scope": item.prediction_scope.value,
                "subject_id": item.subject_id,
            }
        )
    return entries


def _drivers(
    *, prediction: PricePrediction, competitors: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Every explanation factor on the page, strongest first.

    Each driver keeps its own ``kind`` (observed / derived / predicted) and gains
    a ``scope`` so the UI can say *what* the factor was driving. Sorting is by
    absolute weight — direction is a separate axis and a strong "decreases" is
    every bit as informative as a strong "increases" — with the factor name as a
    tie-break so the order is total and the output deterministic.
    """
    collected: list[dict[str, Any]] = []

    def _add(factors: Sequence[ExplanationFactor], scope: str, subject_id: Any) -> None:
        for factor in factors or []:
            entry = factor.to_dict()
            entry["scope"] = scope
            entry["subject_id"] = subject_id
            collected.append(entry)

    _add(
        prediction.explanation_factors,
        prediction.prediction_scope.value,
        prediction.subject_id,
    )
    for competitor in competitors:
        item = competitor.get("prediction")
        if isinstance(item, PricePrediction):
            _add(item.explanation_factors, item.prediction_scope.value, item.subject_id)

    collected.sort(key=lambda f: (-abs(float(f.get("weight") or 0.0)), str(f.get("name"))))
    return collected


def _warnings(
    *,
    prediction: PricePrediction,
    observed: Sequence[Mapping[str, Any]],
    untraceable: int,
    observed_values: Sequence[float],
    ages_days: Sequence[float],
    competitors: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Everything a reader should know before trusting the numbers above."""
    out: list[str] = []
    if prediction.is_suppressed:
        out.append(
            "لا يوجد رقم متوقع: تم حجب التقدير لسبب "
            f"{prediction.suppression_reason.value if prediction.suppression_reason else 'غير محدد'}"
        )
    if untraceable:
        out.append(
            f"{untraceable} صفقة مقارنة لم يُعثر لها على سجل ترسية قابل للتتبع، "
            "فاستُبعدت من الحقائق المُلاحظة"
        )
    missing_lineage = sum(1 for entry in observed if not entry.get("lineage"))
    if missing_lineage:
        out.append(
            f"{missing_lineage} من {len(observed)} حقيقة مُلاحظة بلا سجل مصدر (source_lineage) مُدوَّن"
        )
    if 0 < len(observed_values) < THIN_SAMPLE_THRESHOLD:
        out.append(
            f"عينة المقارنة رقيقة (n={len(observed_values)}): النطاق أوسع مما توحي به دقته الظاهرة"
        )
    if not observed_values:
        out.append("لا توجد صفقات مقارنة قابلة للتتبع لهذا المنافسة")
    median_age = _median(ages_days)
    if median_age is not None and median_age > AGEING_SAMPLE_DAYS:
        out.append(f"متوسط عمر الأدلة {median_age:.0f} يوم: بيانات قديمة نسبياً")
    if prediction.evidence_tier is not None and not prediction.evidence_tier.allows_competitor_prediction:
        out.append(
            f"مستوى الأدلة {prediction.evidence_tier.value}: أسعار المنافسين محجوبة ولا يجوز استنتاجها"
        )
    suppressed_competitors = sum(
        1
        for c in competitors
        if isinstance(c.get("prediction"), PricePrediction) and c["prediction"].is_suppressed
    )
    if suppressed_competitors:
        out.append(
            f"{suppressed_competitors} منافس بلا نطاق سعري مدعوم بالأدلة (محجوب وليس صفراً)"
        )
    return out


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


async def build_explanation(
    conn: Any,
    *,
    prediction: PricePrediction,
    similar: list,
    competitors: list,
) -> dict[str, Any]:
    """Assemble the four-part explanation behind ``prediction``.

    Returns::

        {
          'observed':  [ {label, value, unit, tender_id, evidence_ref, as_of,
                          kind, source_table, source_id, lineage} ],
          'derived':   [ {label, value, method} ],
          'predicted': [ {label, p10, p50, p90, confidence, model_version, ...} ],
          'drivers':   [ explanation-factor dicts, |weight| descending ],
          'data_quality_warnings': [str],
          'suppression': {...} | None,
        }

    Never raises for missing data — a database that answers nothing yields an
    explanation with empty lists and warnings that say so. It *does* raise if the
    guard finds a model output in ``observed``, which is a code defect rather
    than a data condition.
    """
    if not isinstance(prediction, PricePrediction):
        # ValueError, not TypeError: contracts.py treats a bad value and a bad
        # type as one class of input problem, and this module follows it.
        raise ValueError(  # noqa: TRY004
            f"prediction must be a PricePrediction, got {type(prediction).__name__}"
        )
    normalised = _normalise_competitors(competitors or [])

    observed, untraceable, observed_values, ages = await _observed_comparables(
        conn, similar or []
    )
    observed = observed + await _observed_vendors(conn, normalised)
    _assert_observed_are_facts(observed)

    suppression: dict[str, Any] | None = None
    if prediction.is_suppressed:
        suppression = {
            "reason": prediction.suppression_reason.value,
            "scope": prediction.prediction_scope.value,
            "subject_id": prediction.subject_id,
            "evidence_count": prediction.evidence_count,
            "evidence_tier": (
                prediction.evidence_tier.value if prediction.evidence_tier else None
            ),
            "model_version": prediction.model_version,
        }

    return {
        "observed": observed,
        "derived": _derived_entries(
            prediction=prediction,
            similar=similar or [],
            competitors=normalised,
            observed_values=observed_values,
            ages_days=ages,
        ),
        "predicted": _predicted_entries(prediction=prediction, competitors=normalised),
        "drivers": _drivers(prediction=prediction, competitors=normalised),
        "data_quality_warnings": _warnings(
            prediction=prediction,
            observed=observed,
            untraceable=untraceable,
            observed_values=observed_values,
            ages_days=ages,
            competitors=normalised,
        ),
        "suppression": suppression,
        "model_version": MODEL_VERSION,
        "generated_at": prediction.generated_at.isoformat(),
    }


__all__ = [
    "AGEING_SAMPLE_DAYS",
    "OBSERVED_KEYS",
    "OBSERVED_TABLES",
    "THIN_SAMPLE_THRESHOLD",
    "UNIT_NAME",
    "UNIT_SAR",
    "build_explanation",
    "trace_fact",
]
