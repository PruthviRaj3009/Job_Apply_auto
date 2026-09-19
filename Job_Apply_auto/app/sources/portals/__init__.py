"""
Portal adapters.

Importing this package registers every adapter, so `available_sources()` is
complete after a single `import app.sources`.
"""

from . import html_portals, linkedin, naukri  # noqa: F401

__all__ = ["html_portals", "linkedin", "naukri"]
