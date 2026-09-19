"""
Naukri adapter.

Naukri renders its listings from an internal JSON API (`/jobapi/v3/search`),
which the browser calls anyway. Reading that response instead of scraping the
rendered DOM gives structured fields — skills, salary, epoch posting date —
and does not break every time they reskin the site.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ..base import JobSourceAdapter, RawJob, SearchQuery
from ..registry import register

logger = logging.getLogger(__name__)

BASE = "https://www.naukri.com"
#: Naukri returns 20 per page; five pages is a reasonable ceiling per keyword.
MAX_PAGES = 5
PAGE_SIZE = 20


@register
class NaukriAdapter(JobSourceAdapter):
    slug = "naukri"
    display_name = "Naukri"
    kind = "portal"
    request_delay = 4.0

    def build_search_url(self, query: SearchQuery) -> str:
        kw_slug = query.keyword_string.lower().replace(" ", "-").replace(",", "-") or "jobs"
        loc_slug = (query.location or "india").lower().replace(" ", "-").replace(",", "")
        exp = int(query.experience_years or 0)
        return (
            f"{BASE}/{kw_slug}-jobs-in-{loc_slug}"
            f"?experience={exp}&expmax={exp + 2}"
        )

    # ------------------------------------------------------------- parsing

    def parse_listing(self, content: str, *, base_url: str = "") -> list[RawJob]:
        """
        Parse one `/jobapi/v3/search` response body.

        Pure and synchronous: `content` is the JSON text the interceptor
        captured, so this is exercised directly against recorded fixtures.
        """
        try:
            data: Any = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            logger.debug("Naukri: response was not JSON")
            return []

        if not isinstance(data, dict):
            return []

        jobs: list[RawJob] = []
        for entry in data.get("jobDetails") or []:
            if not isinstance(entry, dict):
                continue
            job = self._parse_entry(entry)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse_entry(self, entry: dict) -> RawJob | None:
        title = (entry.get("title") or "").strip()
        jd_url = (entry.get("jdURL") or "").strip()
        if not title or not jd_url:
            return None
        if jd_url and not jd_url.startswith("http"):
            jd_url = BASE + jd_url

        location = ""
        salary_text = ""
        experience_text = ""
        for placeholder in entry.get("placeholders") or []:
            if not isinstance(placeholder, dict):
                continue
            kind = placeholder.get("type")
            label = (placeholder.get("label") or "").strip()
            if kind == "location":
                location = label
            elif kind == "salary":
                # Naukri's own "not disclosed" wording carries no number;
                # keeping it verbatim lets parse_salary correctly find none.
                salary_text = label
            elif kind == "experience":
                experience_text = label

        skills_raw = entry.get("tagsAndSkills") or ""
        skills = [s.strip() for s in skills_raw.split(",") if s.strip()]

        return RawJob(
            title=title,
            url=jd_url,
            source_slug=self.slug,
            company=(entry.get("companyName") or "").strip(),
            location=location,
            description=entry.get("jobDescription") or "",
            salary_text=salary_text,
            experience_text=experience_text,
            external_id=str(entry.get("jobId") or ""),
            posted_at=_parse_created(entry.get("createdDate")),
            # companyApplyJob=True means "apply on the company's own site",
            # i.e. not a Naukri-native flow the engine can drive.
            easy_apply=not bool(entry.get("companyApplyJob")),
            skills=skills,
            raw=entry,
        )

    # ------------------------------------------------------------ fetching

    async def search_jobs(self, page: Any, query: SearchQuery) -> list[RawJob]:
        """
        Walk the paginated search, capturing the API response for each page.

        The route handler fulfils every request with the original response, so
        the page still renders normally — this observes traffic rather than
        replacing it.
        """
        captured: list[str] = []

        async def _intercept(route, request):  # type: ignore[no-untyped-def]
            response = await route.fetch()
            if "jobapi/v3/search" in request.url:
                try:
                    captured.append(await response.text())
                except Exception:  # noqa: BLE001 - a body we cannot read is not fatal
                    logger.debug("Naukri: could not read intercepted body")
            await route.fulfill(response=response)

        await page.context.route("**/jobapi/**", _intercept)
        base_url = self.build_search_url(query)
        found: list[RawJob] = []
        seen_urls: set[str] = set()

        try:
            for page_no in range(1, MAX_PAGES + 1):
                captured.clear()
                url = base_url if page_no == 1 else f"{base_url}&pageNo={page_no}"
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(4000)

                if not captured:
                    break

                page_jobs: list[RawJob] = []
                for body in captured:
                    page_jobs.extend(self.parse_listing(body))

                new = [j for j in page_jobs if j.url not in seen_urls]
                for job in new:
                    seen_urls.add(job.url)
                found.extend(new)

                logger.info("Naukri page %d: %d jobs (total %d)", page_no, len(page_jobs), len(found))
                if len(page_jobs) < PAGE_SIZE or len(found) >= query.limit:
                    break
        finally:
            await page.context.unroute("**/jobapi/**", _intercept)

        return found[: query.limit]

    async def extract_job_description(self, page: Any, url: str) -> str:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)
        for selector in ("section.job-desc", "div.dang-inner-html", "div.job-desc"):
            node = await page.query_selector(selector)
            if node:
                return (await node.inner_text()).strip()
        return ""


def _parse_created(value: object) -> datetime | None:
    """
    Naukri's `createdDate` is an epoch in seconds or milliseconds.

    The magnitude test distinguishes them: anything past 1e11 is milliseconds
    (seconds would be the year 5138).
    """
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    seconds = value if value < 1e11 else value / 1000
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
