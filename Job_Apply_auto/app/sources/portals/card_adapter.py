"""
Shared base for portals whose listings are plain HTML cards.

The original code carried six near-identical 50-line scrape loops that
differed only in their CSS selectors — the same `query_selector_all`, the same
per-field `inner_text()` dance, the same broad `except Exception`. Those
differences are data, so they live in a `CardSelectors` spec and the loop
exists once. Adding a portal becomes a dozen lines of selectors.

Parsing is done with lxml over the page HTML rather than per-element
`await` calls: one round trip instead of six per card, and — more importantly
— `parse_listing()` stays a pure function that can be tested against a saved
HTML fixture with no browser at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lxml import html as lxml_html

from ..base import JobSourceAdapter, RawJob, SearchQuery
from ..normalize import parse_posted_at
from ..registry import register  # noqa: F401  (re-exported for portal modules)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CardSelectors:
    """
    CSS selectors for one portal's result card.

    Each field takes a list of alternatives, tried in order — portals ship
    several layouts at once and A/B test them, so a single selector per field
    is never enough in practice.
    """

    card: list[str]
    title: list[str]
    link: list[str] = field(default_factory=list)
    company: list[str] = field(default_factory=list)
    location: list[str] = field(default_factory=list)
    salary: list[str] = field(default_factory=list)
    posted: list[str] = field(default_factory=list)
    experience: list[str] = field(default_factory=list)


def _first_text(node: Any, selectors: list[str]) -> str:
    """Text of the first matching descendant, or ''."""
    for selector in selectors:
        try:
            found = node.cssselect(selector)
        except Exception:  # noqa: BLE001 - a malformed selector should not kill the run
            logger.debug("Bad selector %r", selector)
            continue
        if found:
            text = found[0].text_content().strip()
            if text:
                return " ".join(text.split())
    return ""


def _first_href(node: Any, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            found = node.cssselect(selector)
        except Exception:  # noqa: BLE001
            continue
        for element in found:
            href = element.get("href")
            if href:
                return href.strip()
    return ""


class CardScrapeAdapter(JobSourceAdapter):
    """
    Base for HTML-card portals. Subclasses set `selectors` and `base_url`,
    and implement `build_search_url`.
    """

    selectors: CardSelectors
    base_url: str = ""
    #: Cards to take from one results page.
    max_cards: int = 25

    def parse_listing(self, content: str, *, base_url: str = "") -> list[RawJob]:
        if not content or not content.strip():
            return []
        try:
            tree = lxml_html.fromstring(content)
        except Exception:  # noqa: BLE001
            logger.warning("%s: could not parse listing HTML", self.slug)
            return []

        cards: list[Any] = []
        for selector in self.selectors.card:
            try:
                cards = tree.cssselect(selector)
            except Exception:  # noqa: BLE001
                continue
            if cards:
                break

        root = base_url or self.base_url
        jobs: list[RawJob] = []
        for card in cards[: self.max_cards]:
            job = self._parse_card(card, root)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse_card(self, card: Any, root: str) -> RawJob | None:
        title = _first_text(card, self.selectors.title)
        if not title or len(title) < 3:
            return None

        href = _first_href(card, self.selectors.link or self.selectors.title)
        if href and not href.startswith("http"):
            href = root.rstrip("/") + "/" + href.lstrip("/")
        if not href:
            return None

        posted_text = _first_text(card, self.selectors.posted)
        return RawJob(
            title=title,
            url=href.split("?")[0],
            source_slug=self.slug,
            company=_first_text(card, self.selectors.company),
            location=_first_text(card, self.selectors.location),
            # Left empty rather than "Not listed": the normalizer must be able
            # to tell "no salary published" from a literal string it would
            # then try to parse.
            salary_text=_first_text(card, self.selectors.salary),
            experience_text=_first_text(card, self.selectors.experience),
            posted_at=parse_posted_at(posted_text),
            raw={"posted_text": posted_text},
        )

    async def search_jobs(self, page: Any, query: SearchQuery) -> list[RawJob]:
        url = self.build_search_url(query)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2500)

        if await detect_captcha(page):
            # Raising beats returning a placeholder row: the caller records a
            # source-level failure and the rest of the run continues, instead
            # of a fake "[CAPTCHA]" job flowing into matching.
            raise CaptchaEncountered(
                f"{self.display_name} presented a CAPTCHA. Complete it manually "
                f"(`jobpilot session login {self.slug}`) and retry."
            )

        jobs = self.parse_listing(await page.content(), base_url=self.base_url)
        return jobs[: query.limit]


class CaptchaEncountered(Exception):
    """
    A source demanded human verification.

    Never bypassed (section 12): the run stops for that source and asks the
    user to complete the challenge themselves.
    """


async def detect_captcha(page: Any) -> bool:
    """
    Look for a challenge in *visible* text.

    Checking page source instead produces constant false positives, because
    analytics bundles on these sites mention "recaptcha" on every page.
    """
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:  # noqa: BLE001
        return False
    return any(
        marker in text
        for marker in (
            "recaptcha",
            "hcaptcha",
            "cf-challenge",
            "verify you are human",
            "are you a robot",
            "security check",
        )
    )
