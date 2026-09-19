"""
Shared service layer.

The API, CLI and MCP server are all thin wrappers over these, so business
logic exists in exactly one place (section 3).
"""

from .analysis import AnalysisService
from .analytics import AnalyticsService, DashboardCounts
from .discovery import DiscoveryReport, DiscoveryService
from .profile_loader import ProfileFormatError, load_profile, load_profile_file
from .resume_service import ResumeService

__all__ = [
    "AnalysisService",
    "AnalyticsService",
    "DashboardCounts",
    "DiscoveryReport",
    "DiscoveryService",
    "ProfileFormatError",
    "ResumeService",
    "load_profile",
    "load_profile_file",
]
