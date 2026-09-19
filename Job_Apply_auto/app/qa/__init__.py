"""
Application question knowledge base and sensitive-question policy.

Sections 10, 11 and 12: answer from the user's own data when confident,
otherwise pause and ask — and always ask for anything sensitive.
"""

from .knowledge_base import AnswerResolution, KnowledgeBase, normalize_question
from .sensitive import (
    SENSITIVE_TOPICS,
    SensitivityVerdict,
    categorize,
    classify_sensitivity,
    is_sensitive,
)

__all__ = [
    "SENSITIVE_TOPICS",
    "AnswerResolution",
    "KnowledgeBase",
    "SensitivityVerdict",
    "categorize",
    "classify_sensitivity",
    "is_sensitive",
    "normalize_question",
]
