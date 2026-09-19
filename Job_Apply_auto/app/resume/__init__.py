"""
Truthful job-specific resume generation (sections 7, 8, 9).

The pipeline is: GapAnalyzer decides what may be said -> ResumeGenerator
renders only that -> compile_tex produces a PDF -> validate_* proves nothing
unsupported got through.
"""

from .compiler import CompileResult, compile_tex, count_pages, latex_available
from .gap_analysis import (
    TRANSFERABLE_GROUPS,
    GapAnalyzer,
    GapReport,
    KeywordStatus,
    KeywordVerdict,
)
from .generator import GeneratedResume, ResumeGenerator
from .validator import ValidationReport, validate_pdf, validate_tex

__all__ = [
    "TRANSFERABLE_GROUPS",
    "CompileResult",
    "GapAnalyzer",
    "GapReport",
    "GeneratedResume",
    "KeywordStatus",
    "KeywordVerdict",
    "ResumeGenerator",
    "ValidationReport",
    "compile_tex",
    "count_pages",
    "latex_available",
    "validate_pdf",
    "validate_tex",
]
