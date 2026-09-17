"""BoQ line -> catalogue item matching (PRD "Thaqip for Contractors" §5.3,
T-MATCH-01).

Honesty note: the PRD calls this "AI-assisted". This implementation is a
deterministic lexical-similarity matcher (token overlap + sequence ratio),
not a trained model or an LLM/embedding call — there is no embedding
pipeline in this codebase yet (doc_chunks.embedding exists in the schema but
is never populated by any code), and standing one up (an external API,
credentials, cost, latency) is out of scope for this slice. This module is
deliberately named and documented as what it actually is so nothing here
overstates its own sophistication; upgrading the scoring function to use
real embeddings later is a drop-in replacement of `_score` alone — the
public contract (kind/confidence/candidates, human confirmation required)
does not change.

Pure functions only: no I/O, no DB. Callers (services/console's upload
handler) fetch `catalogue_items` themselves and pass them in.

Per T-MATCH-01, a suggestion is only ever emitted with >= 2 candidates —
never zero, never one. If the catalogue has fewer than 2 unit-compatible
items, or nothing clears the confidence floor, `suggest_match` returns None
rather than force a weak or lonely guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_TOKEN_RE = re.compile(r"[^\W\d_]{3,}|\d+(?:\.\d+)?", re.UNICODE)

MIN_SUGGEST_SCORE = 0.35
MAX_CANDIDATES = 2


@dataclass(frozen=True)
class CatalogueItemLike:
    """Duck-typed subset of a `catalogue_items` row this module reads."""
    id: int
    code: str | None
    name_ar: str
    unit: str


@dataclass(frozen=True)
class MatchCandidate:
    kind: str  # always "suggested" — a confirmed match is not a "candidate" any more
    catalogue_item_id: int
    code: str | None
    name_ar: str
    unit: str
    score: float


def _normalize_unit(unit: str | None) -> str:
    return (unit or "").strip().lower()


def _tokenize(text: str) -> set[str]:
    t = (text or "").translate(_ARABIC_INDIC_DIGITS)
    return set(_TOKEN_RE.findall(t))


def _score(line_text: str, item_name: str) -> float:
    a, b = _tokenize(line_text), _tokenize(item_name)
    if not a or not b:
        return 0.0
    overlap = len(a & b) / len(a | b)
    ratio = SequenceMatcher(None, line_text, item_name).ratio()
    return round(0.6 * overlap + 0.4 * ratio, 4)


def suggest_match(
    description: str,
    spec: str | None,
    unit: str | None,
    catalogue_items: list[CatalogueItemLike],
) -> list[MatchCandidate] | None:
    """Returns the top-2 candidates (ranked, highest score first), or None
    when no honest suggestion can be made."""
    line_unit = _normalize_unit(unit)
    line_text = f"{description or ''} {spec or ''}".strip()
    if not line_text or not line_unit:
        return None

    compatible = [c for c in catalogue_items if _normalize_unit(c.unit) == line_unit]
    if len(compatible) < MAX_CANDIDATES:
        return None  # can't honestly show 2 candidates if fewer than 2 exist

    scored = sorted(
        (
            MatchCandidate(
                kind="suggested", catalogue_item_id=c.id, code=c.code,
                name_ar=c.name_ar, unit=c.unit, score=_score(line_text, c.name_ar),
            )
            for c in compatible
        ),
        key=lambda m: m.score,
        reverse=True,
    )
    top = scored[:MAX_CANDIDATES]
    if top[0].score < MIN_SUGGEST_SCORE:
        return None
    return top
