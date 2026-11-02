"""Centralized research-relevance thresholds and labels.

Single source of truth for mapping 0..1 scores to coarse levels. The
TypeScript mirror lives in web/lib/relevance.ts (levels only; keep the
cutoffs below in sync when tuning).
"""

from __future__ import annotations

from typing import Literal

RelevanceLevel = Literal["high", "related", "context"]

HIGH_THRESHOLD = 0.75
RELATED_THRESHOLD = 0.40

IMPORTANCE_HIGH_THRESHOLD = 0.66
IMPORTANCE_MEDIUM_THRESHOLD = 0.33


def level_for_score(score: float) -> RelevanceLevel:
    """Map a 0..1 relevance score to its coarse level."""
    if score >= HIGH_THRESHOLD:
        return "high"
    if score >= RELATED_THRESHOLD:
        return "related"
    return "context"


def importance_label(score: float) -> Literal["High", "Medium", "Low"]:
    """Map a 0..1 paper-importance score to a UI label (no raw floats)."""
    if score >= IMPORTANCE_HIGH_THRESHOLD:
        return "High"
    if score >= IMPORTANCE_MEDIUM_THRESHOLD:
        return "Medium"
    return "Low"
