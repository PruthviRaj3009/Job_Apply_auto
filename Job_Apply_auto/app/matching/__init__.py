"""AI matching: job vs. master profile."""

from .engine import WEIGHTS, MatchingEngine, ProfileSnapshot
from .result import DimensionScore, DimensionVerdict, MatchResult

__all__ = [
    "WEIGHTS",
    "DimensionScore",
    "DimensionVerdict",
    "MatchResult",
    "MatchingEngine",
    "ProfileSnapshot",
]
