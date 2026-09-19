"""
Company career-page adapters (section 18).

Almost no company writes its own careers site. They run Greenhouse, Lever,
Ashby, SmartRecruiters, Workday or Recruitee, each of which has a stable
public JSON API keyed by a company slug. Supporting the six ATS platforms
covers vastly more companies than writing one adapter per employer ever
could, and their APIs change far less often than a rendered page.

`detect_ats` finds which one a company uses from a careers URL, so adding a
company is usually just pasting its careers link.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..base import JobSourceAdapter, RawJob, SearchQuery
from ..normalize import parse_posted_at
from ..registry import register

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ATSPlatform:
    key: str
    name: str
    #: Format string taking the company slug.
    api_template: str
    #: Patterns that identify this ATS from a careers URL.
    url_patterns: tuple[str, ...]


ATS_PLATFORMS: tuple[ATSPlatform, ...] = (
    ATSPlatform(
        "greenhouse", "Greenhouse",
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        (r"boards\.greenhouse\.io/([a-z0-9_-]+)", r"job-boards\.greenhouse\.io/([a-z0-9_-]+)"),
    ),
    ATSPlatform(
        "lever", "Lever",
        "https://api.lever.co/v0/postings/{slug}?mode=json",
        (r"jobs\.lever\.co/([a-z0-9_-]+)",),
    ),
    ATSPlatform(
        "ashby", "Ashby",
        "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
        (r"jobs\.ashbyhq\.com/([a-z0-9_-]+)",),
    ),
    ATSPlatform(
        "smartrecruiters", "SmartRecruiters",
        "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
        (r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)", r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ),
    ATSPlatform(
        "recruitee", "Recruitee",
        "https://{slug}.recruitee.com/api/offers/",
        (r"([a-z0-9_-]+)\.recruitee\.com",),
    ),
    ATSPlatform(
        "workable", "Workable",
        "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
        (r"apply\.workable\.com/([a-z0-9_-]+)",),
    ),
)

_PLATFORMS_BY_KEY = {p.key: p for p in ATS_PLATFORMS}


def detect_ats(careers_url: str) -> tuple[str, str] | None:
    """
    Identify the ATS and company slug behind a careers URL.

    Returns (platform_key, slug), or None when the URL is a bespoke careers
    page no ATS adapter can read — a real outcome, and the caller falls back
    to the generic HTML adapter.
    """
    if not careers_url:
        return None
    for platform in ATS_PLATFORMS:
        for pattern in platform.url_patterns:
            if match := re.search(pattern, careers_url, re.IGNORECASE):
                return platform.key, match.group(1)
    return None


@register
class ATSCareerAdapter(JobSourceAdapter):
    """
    Reads a company's postings from whichever ATS it uses.

    Configured per company:

        get_adapter("careerpage", {
            "company": "Acme",
            "platform": "greenhouse",
            "slug": "acme",
        })

    `supports_search` is False because these APIs return the company's whole
    board rather than answering a query — filtering happens on our side, which
    is also why career pages are excellent at "detect newly posted jobs".
    """

    slug = "careerpage"
    display_name = "Company career page"
    kind = "careerpage"
    supports_search = False
    request_delay = 2.0

    @property
    def company(self) -> str:
        return self.config.get("company", "")

    @property
    def platform(self) -> ATSPlatform | None:
        return _PLATFORMS_BY_KEY.get(self.config.get("platform", ""))

    def build_search_url(self, query: SearchQuery) -> str:
        platform = self.platform
        board_slug = self.config.get("slug", "")
        if platform is None or not board_slug:
            # A bespoke page: fall back to whatever URL was configured.
            return self.config.get("careers_url", "")
        return platform.api_template.format(slug=board_slug)

    # ------------------------------------------------------------- parsing

    def parse_listing(self, content: str, *, base_url: str = "") -> list[RawJob]:
        """
        Parse an ATS board response.

        Each platform has its own envelope, so parsing dispatches on the
        configured platform rather than guessing from the payload shape.
        """
        if not content or not content.strip():
            return []
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            logger.debug("%s board response was not JSON", self.company)
            return []

        key = self.config.get("platform", "")
        parser = {
            "greenhouse": self._parse_greenhouse,
            "lever": self._parse_lever,
            "ashby": self._parse_ashby,
            "smartrecruiters": self._parse_smartrecruiters,
            "recruitee": self._parse_recruitee,
            "workable": self._parse_workable,
        }.get(key)

        if parser is None:
            logger.warning("No parser for ATS platform %r", key)
            return []
        try:
            return [job for job in parser(data) if job.is_usable]
        except Exception:  # noqa: BLE001 - a changed API must not kill the run
            logger.exception("Failed to parse %s board for %s", key, self.company)
            return []

    def _job(self, **kwargs: Any) -> RawJob:
        """RawJob with the company and source filled in."""
        kwargs.setdefault("company", self.company)
        kwargs.setdefault("source_slug", self.slug)
        # Career-page applications are native flows, so they are drivable.
        kwargs.setdefault("easy_apply", True)
        return RawJob(**kwargs)

    def _parse_greenhouse(self, data: Any) -> list[RawJob]:
        return [
            self._job(
                title=entry.get("title", ""),
                url=entry.get("absolute_url", ""),
                external_id=str(entry.get("id", "")),
                location=(entry.get("location") or {}).get("name", ""),
                description=_strip_html(entry.get("content", "")),
                posted_at=_parse_iso(entry.get("updated_at") or entry.get("first_published")),
                raw=entry,
            )
            for entry in (data.get("jobs") or [])
        ]

    def _parse_lever(self, data: Any) -> list[RawJob]:
        # Lever returns a bare array rather than an envelope object.
        return [
            self._job(
                title=entry.get("text", ""),
                url=entry.get("hostedUrl", ""),
                application_url=entry.get("applyUrl", ""),
                external_id=str(entry.get("id", "")),
                location=(entry.get("categories") or {}).get("location", ""),
                description=_strip_html(
                    entry.get("descriptionPlain") or entry.get("description", "")
                ),
                posted_at=_parse_epoch_ms(entry.get("createdAt")),
                raw=entry,
            )
            for entry in (data if isinstance(data, list) else [])
        ]

    def _parse_ashby(self, data: Any) -> list[RawJob]:
        return [
            self._job(
                title=entry.get("title", ""),
                url=entry.get("jobUrl", ""),
                external_id=str(entry.get("id", "")),
                location=entry.get("location", ""),
                description=_strip_html(entry.get("descriptionHtml", "")),
                posted_at=_parse_iso(entry.get("publishedAt")),
                raw=entry,
            )
            for entry in (data.get("jobs") or [])
        ]

    def _parse_smartrecruiters(self, data: Any) -> list[RawJob]:
        jobs: list[RawJob] = []
        for entry in data.get("content") or []:
            location = entry.get("location") or {}
            city = ", ".join(
                part for part in (location.get("city"), location.get("country")) if part
            )
            jobs.append(
                self._job(
                    title=entry.get("name", ""),
                    url=(entry.get("ref") or {}).get("jobAd", "")
                    or f"https://jobs.smartrecruiters.com/{self.config.get('slug','')}/{entry.get('id','')}",
                    external_id=str(entry.get("id", "")),
                    location=city,
                    posted_at=_parse_iso(entry.get("releasedDate")),
                    raw=entry,
                )
            )
        return jobs

    def _parse_recruitee(self, data: Any) -> list[RawJob]:
        return [
            self._job(
                title=entry.get("title", ""),
                url=entry.get("careers_url") or entry.get("careers_apply_url", ""),
                external_id=str(entry.get("id", "")),
                location=", ".join(
                    part for part in (entry.get("city"), entry.get("country")) if part
                ),
                description=_strip_html(entry.get("description", "")),
                posted_at=_parse_iso(entry.get("published_at")),
                raw=entry,
            )
            for entry in (data.get("offers") or [])
        ]

    def _parse_workable(self, data: Any) -> list[RawJob]:
        return [
            self._job(
                title=entry.get("title", ""),
                url=entry.get("url") or entry.get("application_url", ""),
                external_id=str(entry.get("shortcode") or entry.get("id", "")),
                location=", ".join(
                    part
                    for part in (
                        (entry.get("location") or {}).get("city"),
                        (entry.get("location") or {}).get("country"),
                    )
                    if part
                ),
                description=_strip_html(entry.get("description", "")),
                posted_at=_parse_iso(entry.get("published_on")),
                raw=entry,
            )
            for entry in (data.get("jobs") or [])
        ]

    # ------------------------------------------------------------ fetching

    async def search_jobs(self, page: Any, query: SearchQuery) -> list[RawJob]:
        """
        Fetch the company's board.

        Uses the browser's own request context rather than a separate HTTP
        client: it reuses the session and honours the same proxy and TLS
        settings, and it keeps the dependency surface to Playwright alone.
        """
        url = self.build_search_url(query)
        if not url:
            return []

        response = await page.context.request.get(url, timeout=30_000)
        if not response.ok:
            logger.warning(
                "%s board returned %s for %s", self.company, response.status, url
            )
            return []

        jobs = self.parse_listing(await response.text())
        return jobs[: query.limit]

    async def extract_job_description(self, page: Any, url: str) -> str:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1500)
        for selector in (
            "div#content", "div.job__description", "div[class*='description']",
            "section[class*='description']", "main",
        ):
            node = await page.query_selector(selector)
            if node:
                return (await node.inner_text()).strip()
        return ""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&(?:nbsp|amp|lt|gt|quot|#39|#x27);")
_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&#x27;": "'",
}


def _strip_html(text: str) -> str:
    """
    Plain text from an HTML description.

    ATS APIs return escaped HTML; block-level tags become newlines first so
    the JD parser can still see bullets and headings, which it needs to split
    requirements from responsibilities.
    """
    if not text:
        return ""
    text = re.sub(r"</(?:p|div|li|h[1-6]|tr)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "\n• ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = _ENTITY_RE.sub(lambda m: _ENTITIES.get(m.group(0), " "), text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return parse_posted_at(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_epoch_ms(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
