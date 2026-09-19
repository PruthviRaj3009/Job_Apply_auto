"""
LinkedIn adapter.

Two things drive the shape of this adapter:

1. LinkedIn **virtualizes** its results list — cards scroll out of the DOM
   rather than accumulating — so extraction has to happen on every scroll
   step and accumulate by URL, not once at the end.
2. It ties sessions to a browser fingerprint, so a cookie-only replay gets
   logged out. The engine drives a persistent browser profile instead
   (see `app.browser`).

`f_TPR` is LinkedIn's own server-side recency filter — the same one behind the
"Past 24 hours" pill. Using it is far more reliable than filtering afterwards,
because the scraper only ever sees the first page or two of results.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from ..base import JobSourceAdapter, RawJob, SearchQuery
from ..normalize import parse_posted_at
from ..registry import register

logger = logging.getLogger(__name__)

BASE = "https://www.linkedin.com"
#: Opening each result to read its full JD costs seconds per job; cap it.
JD_FETCH_LIMIT = 15

#: Extraction runs inside the page on every scroll step. Selectors are listed
#: broadest-last because LinkedIn ships several list layouts concurrently.
_EXTRACT_JS = """() => {
    const out = [];
    const cards = document.querySelectorAll(
        'li.jobs-search-results__list-item, ' +
        'div.job-card-container, ' +
        'ul.jobs-search__results-list > li, ' +
        'ul.scaffold-layout__list-container > li'
    );
    for (const card of cards) {
        const linkEl = card.querySelector('a[href*="/jobs/view/"]');
        if (!linkEl) continue;
        const pick = (sel) => { const el = card.querySelector(sel); return el ? el.textContent.trim() : ''; };
        const title = pick(
            'a.job-card-container__link, a.job-card-list__title--link, ' +
            'h3, a.job-card-list__title, h3.base-search-card__title'
        );
        const company = pick(
            'div.artdeco-entity-lockup__subtitle, h4, h4.base-search-card__subtitle, ' +
            'a[data-tracking-control-name*="company"], span.job-card-container__primary-description'
        );
        const location = pick(
            'div.artdeco-entity-lockup__caption li, div.artdeco-entity-lockup__caption span, ' +
            'span.job-search-card__location, li.job-card-container__metadata-item, ' +
            'span[class*="location"], span.job-card-container__metadata-wrapper'
        );
        const posted = pick('time, span[class*="listed-date"]');
        const href = linkEl.getAttribute('href') || '';
        if (title && title.length > 2) out.push({title, company, location, href, posted});
    }
    return out;
}"""

_JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/]*-)?(\d+)")


class LinkedInBlocked(Exception):
    """
    Raised when LinkedIn returns a CAPTCHA or bounces us to login.

    A distinct exception rather than a fake job row: the old code returned a
    placeholder `JobResult` titled "[CAPTCHA] ...", which flowed downstream as
    if it were a real posting.
    """


@register
class LinkedInAdapter(JobSourceAdapter):
    slug = "linkedin"
    display_name = "LinkedIn"
    kind = "portal"
    request_delay = 5.0

    def build_search_url(self, query: SearchQuery) -> str:
        params = {
            "keywords": query.keyword_string,
            "location": query.location or "India",
            "sortBy": "DD",  # most recent first
            "f_TPR": f"r{max(query.max_days_old, 1) * 86_400}",
        }
        if query.easy_apply_only:
            params["f_AL"] = "true"
        if query.remote_only:
            params["f_WT"] = "2"  # LinkedIn's remote workplace-type code
        return f"{BASE}/jobs/search/?" + urllib.parse.urlencode(params)

    # ------------------------------------------------------------- parsing

    def parse_listing(self, content: str, *, base_url: str = "") -> list[RawJob]:
        """
        Build RawJobs from the JS extractor's output.

        `content` is the JSON array the page returned, so this stays pure and
        fixture-testable even though the extraction itself runs in-browser.
        """
        import json

        try:
            entries = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(entries, list):
            return []
        return [j for j in (self._parse_card(e) for e in entries) if j is not None]

    def _parse_card(self, entry: object) -> RawJob | None:
        if not isinstance(entry, dict):
            return None
        title = (entry.get("title") or "").strip()
        href = (entry.get("href") or "").strip()
        if not title or not href:
            return None

        url = href if href.startswith("http") else BASE + href
        # Strip tracking query parameters so the same job found twice has the
        # same URL.
        url = url.split("?")[0]
        job_id = m.group(1) if (m := _JOB_ID_RE.search(url)) else ""

        return RawJob(
            title=title,
            url=url,
            source_slug=self.slug,
            company=(entry.get("company") or "").strip(),
            location=(entry.get("location") or "").strip(),
            external_id=job_id,
            posted_at=parse_posted_at(entry.get("posted") or ""),
            # The search URL carries f_AL when easy-apply-only was requested,
            # so everything returned under it qualifies.
            easy_apply=bool(entry.get("easy_apply", True)),
            raw=entry,
        )

    # ------------------------------------------------------------ fetching

    async def search_jobs(self, page: Any, query: SearchQuery) -> list[RawJob]:
        import json

        url = self.build_search_url(query)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(5000)

        if await _is_blocked(page):
            raise LinkedInBlocked(
                "LinkedIn returned a CAPTCHA or login wall. Log in manually "
                "(`jobpilot session login linkedin`) and retry."
            )

        accumulated: dict[str, RawJob] = {}
        for step in range(12):
            batch = self.parse_listing(json.dumps(await page.evaluate(_EXTRACT_JS)))
            for job in batch:
                accumulated.setdefault(job.url, job)
            if len(accumulated) >= query.limit:
                break
            # Scroll the results panel, not the window: the list lives in its
            # own internally-scrollable container.
            await page.evaluate(
                """() => {
                    const panel = document.querySelector(
                        'div.jobs-search-results-list, ul.scaffold-layout__list-container'
                    );
                    (panel || document.scrollingElement).scrollBy(0, 800);
                }"""
            )
            await page.wait_for_timeout(1200)
            logger.debug("LinkedIn scroll %d: %d unique jobs", step, len(accumulated))

        return list(accumulated.values())[: query.limit]

    async def get_job_details(self, page: Any, job: RawJob) -> RawJob:
        job.description = await self.extract_job_description(page, job.url)
        return job

    async def extract_job_description(self, page: Any, url: str) -> str:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2500)
        for selector in (
            "div.jobs-description__content",
            "div.show-more-less-html__markup",
            "section.description",
            "div.jobs-box__html-content",
        ):
            node = await page.query_selector(selector)
            if node:
                return (await node.inner_text()).strip()
        return ""


async def _is_blocked(page: Any) -> bool:
    """CAPTCHA or login redirect. Checks visible text, not page source."""
    if "login" in page.url or "checkpoint" in page.url or "authwall" in page.url:
        return True
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:  # noqa: BLE001
        return False
    return any(
        marker in text
        for marker in ("recaptcha", "hcaptcha", "verify you are human", "security check")
    )
