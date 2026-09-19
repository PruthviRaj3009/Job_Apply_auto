"""Company career-page adapters (section 18)."""

from .ats import ATS_PLATFORMS, ATSCareerAdapter, detect_ats

__all__ = ["ATS_PLATFORMS", "ATSCareerAdapter", "detect_ats"]
