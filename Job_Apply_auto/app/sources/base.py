"""
The JobSource interface every portal and career page implements (section 4).

Design rule: **fetching is separated from parsing.** Each adapter exposes a
pure `parse_listing()` / `parse_detail()` that turns already-fetched content
into `RawJob`s, and a thin async method that drives the browser. That split is
what makes discovery testable without a live portal — the parsers are
exercised against recorded fixtures, which is the only honest way to test a
scraper (section 22: "use mocked/test browser pages where possible").
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..models.enums import EmploymentType, WorkMode


@dataclass(slots=True)
class SearchQuery:
    """
    What to look for. Built from the user's profile and preferences, then
    handed to every enabled adapter (section 5).
    """

    keywords: Sequence[str] = ()
    location: str = ""
    remote_only: bool = False
    experience_years: float | None = None
    max_days_old: int = 30
    easy_apply_only: bool = False
    employment_types: Sequence[EmploymentType] = ()
    salary_min: float | None = None
    companies: Sequence[str] = ()
    excluded_keywords: Sequence[str] = ()
    excluded_companies: Sequence[str] = ()
    #: Hard cap per adapter per run, so one chatty source cannot dominate.
    limit: int = 100

    @property
    def keyword_string(self) -> str:
        return " ".join(self.keywords)


@dataclass(slots=True)
class RawJob:
    """
    A job as a source reported it, before normalization.

    Deliberately loose: fields the source did not provide stay empty rather
    than being guessed. `normalize()` decides what a missing value means; an
    adapter inventing one would launder a gap into a fact.
    """

    title: str
    url: str
    source_slug: str
    company: str = ""
    location: str = ""
    description: str = ""
    salary_text: str = ""
    external_id: str = ""
    application_url: str = ""
    posted_at: datetime | None = None
    posted_days_ago: int | None = None
    easy_apply: bool = False
    skills: list[str] = field(default_factory=list)
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    work_mode: WorkMode = WorkMode.UNKNOWN
    experience_text: str = ""
    #: Whatever the source actually returned, kept so a parser fix can be
    #: replayed without re-scraping.
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()
        self.company = (self.company or "").strip()
        self.location = (self.location or "").strip()
        self.url = (self.url or "").strip()

    @property
    def is_usable(self) -> bool:
        """A job with no title or no URL cannot be acted on."""
        return bool(self.title and self.url)


class JobSourceAdapter(abc.ABC):
    """
    Base for every source. Subclasses implement the five methods named in
    section 4; the rest of the platform only ever talks to this interface.
    """

    #: Registry key, e.g. "linkedin".
    slug: str = ""
    display_name: str = ""
    #: "portal" or "careerpage".
    kind: str = "portal"
    #: Seconds to wait between requests to this source.
    request_delay: float = 3.0
    #: Whether this source can be searched at all, or only polled for its
    #: own listing page (most single-company career pages).
    supports_search: bool = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    # ------------------------------------------------------------ interface

    @abc.abstractmethod
    def build_search_url(self, query: SearchQuery) -> str:
        """Translate a SearchQuery into this source's own search URL."""

    @abc.abstractmethod
    def parse_listing(self, content: str, *, base_url: str = "") -> list[RawJob]:
        """
        Turn a fetched listing page into RawJobs.

        `content` is HTML, or a JSON string for sources with a usable internal
        API. Pure and synchronous, so it can be tested against a fixture.
        """

    @abc.abstractmethod
    async def search_jobs(self, page: Any, query: SearchQuery) -> list[RawJob]:
        """Drive the browser and return this source's results for `query`."""

    # ------------------------------------------------- optional refinements

    async def get_job_details(self, page: Any, job: RawJob) -> RawJob:
        """
        Enrich a listing-level job by opening it.

        Default: unchanged. Sources whose listing already carries the full
        description (Naukri's API, for one) need no override.
        """
        return job

    async def extract_job_description(self, page: Any, url: str) -> str:
        """Full JD text for a posting. Empty when unavailable."""
        return ""

    def get_application_url(self, job: RawJob) -> str:
        """
        Where to actually apply. Falls back to the posting URL, which is
        correct for every easy-apply flow.
        """
        return job.application_url or job.url

    def parse_detail(self, content: str, job: RawJob) -> RawJob:
        """Pure counterpart to get_job_details, for fixture-based tests."""
        return job

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.slug}>"
