"""
Job discovery sources.

`import app.sources` registers every built-in adapter; use `get_adapter()` to
instantiate one and `available_sources()` to list them.
"""

from . import careers, portals  # noqa: F401  - registers the built-in adapters
from .base import JobSourceAdapter, RawJob, SearchQuery
from .careers import ATS_PLATFORMS, detect_ats
from .filters import FilterVerdict, apply_filters, evaluate
from .normalize import (
    days_since,
    detect_employment_type,
    detect_work_mode,
    job_fingerprint,
    normalize,
    normalize_company,
    normalize_location,
    normalize_title,
    parse_experience,
    parse_posted_at,
    parse_salary,
)
from .registry import available_sources, get_adapter, is_registered, register

__all__ = [
    "ATS_PLATFORMS",
    "FilterVerdict",
    "JobSourceAdapter",
    "RawJob",
    "SearchQuery",
    "apply_filters",
    "available_sources",
    "days_since",
    "detect_ats",
    "detect_employment_type",
    "detect_work_mode",
    "evaluate",
    "get_adapter",
    "is_registered",
    "job_fingerprint",
    "normalize",
    "normalize_company",
    "normalize_location",
    "normalize_title",
    "parse_experience",
    "parse_posted_at",
    "parse_salary",
    "register",
]
