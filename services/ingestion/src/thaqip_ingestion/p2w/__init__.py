"""Price-to-Win package: shared contracts, features, models and calibration.

`contracts` is the vocabulary every other P2W module and the console API import.
It is pure python by design: the honesty invariants (suppress rather than
extrapolate, keep observed/derived/predicted distinct, always carry the evidence)
are enforced there so they cannot drift per call site.

Supersedes the single-point heuristic in ``thaqip_ingestion.pricing_engine``,
which is left in place and untouched.
"""
from .contracts import (
    DIRECTIONS,
    FACTOR_KINDS,
    FRESHNESS_HORIZON_DAYS,
    MODEL_VERSION,
    EvidenceTier,
    ExplanationFactor,
    PredictionScope,
    PricePrediction,
    Quantiles,
    SuppressionReason,
    freshness_score,
)

__all__ = [
    "DIRECTIONS",
    "FACTOR_KINDS",
    "FRESHNESS_HORIZON_DAYS",
    "MODEL_VERSION",
    "EvidenceTier",
    "ExplanationFactor",
    "PredictionScope",
    "PricePrediction",
    "Quantiles",
    "SuppressionReason",
    "freshness_score",
]
