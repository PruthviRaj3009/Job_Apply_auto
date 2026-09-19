"""
Playwright browser lifecycle (section 13).

Playwright is the single browser framework, replacing the Selenium/Playwright
split the audit found. Firefox is the default because Chromium's TLS
fingerprint is blocked outright by several job portals.

Sessions are per-source persistent profiles rather than cookie jars. LinkedIn
in particular ties a session to browser fingerprint and local storage, so a
cookie-only replay logs straight back out — which is why the old code kept a
separate persistent-profile path just for it. Using profiles everywhere
removes the special case.

Nothing here bypasses a security control. `detect_challenge` reports a
CAPTCHA or MFA prompt so the caller can stop and hand over to the user
(section 12); there is no solver, and there never should be.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Text that means a human has to take over.
CHALLENGE_MARKERS = (
    "recaptcha",
    "hcaptcha",
    "cf-challenge",
    "verify you are human",
    "are you a robot",
    "security check",
    "unusual activity",
    "enter the code we sent",
    "two-factor",
    "verification code",
    "confirm your identity",
)


class ChallengeEncountered(Exception):
    """
    A source demanded human verification.

    Deliberately an exception rather than a return value: it must propagate
    past every convenience wrapper, and a caller that forgets to check a
    boolean would carry on driving a blocked page.
    """

    def __init__(self, source: str, url: str = "") -> None:
        self.source = source
        self.url = url
        super().__init__(
            f"{source} is asking for human verification"
            + (f" at {url}" if url else "")
            + ". Complete it yourself with `jobpilot login "
            f"{source}`, then retry. This will not be bypassed automatically."
        )


class BrowserManager:
    """
    Owns the Playwright instance and hands out pages.

    One browser per process; one persistent context per source. Contexts are
    cached because launching Firefox takes seconds and a discovery run touches
    the same source repeatedly.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._playwright: Any = None
        self._contexts: dict[str, Any] = {}

    async def start(self) -> None:
        if self._playwright is not None:
            return
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        logger.debug("Playwright started")

    async def stop(self) -> None:
        for source, context in list(self._contexts.items()):
            try:
                await context.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("Could not close context for %s", source)
        self._contexts.clear()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def context_for(self, source: str) -> Any:
        """
        Persistent browser context for a source, created on first use.

        The profile directory is what carries the login, so it must be stable
        across runs — that is the whole reason sessions survive a restart.
        """
        if source in self._contexts:
            return self._contexts[source]

        await self.start()
        profile_dir = Path(self.settings.browser_profile_dir) / source
        profile_dir.mkdir(parents=True, exist_ok=True)

        browser_type = getattr(self._playwright, self.settings.browser)
        context = await browser_type.launch_persistent_context(
            str(profile_dir),
            headless=self.settings.headless,
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            ignore_https_errors=True,
        )
        context.set_default_timeout(self.settings.page_timeout_ms)
        self._contexts[source] = context
        return context

    @asynccontextmanager
    async def page(self, source: str):
        """
        A page for a source, closed on exit.

        The context is deliberately *not* closed — it holds the login, and
        tearing it down after every use would mean logging in again each time.
        """
        context = await self.context_for(source)
        page = context.pages[0] if context.pages else await context.new_page()
        opened_here = not context.pages
        try:
            yield page
        finally:
            if opened_here:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()


async def detect_challenge(page: Any) -> bool:
    """
    Whether the page is asking for human verification.

    Checks visible body text, not page source: analytics bundles on these
    sites mention "recaptcha" on every page, so scanning the HTML flags
    everything.
    """
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:  # noqa: BLE001 - an unreadable page is not a challenge
        return False
    return any(marker in text for marker in CHALLENGE_MARKERS)


async def interactive_login(source: str, url: str, settings: Settings | None = None) -> dict:
    """
    Open a visible browser so the user can log in themselves.

    The window stays open until they close it rather than timing out after a
    fixed wait — the old code waited exactly two minutes and then saved
    whatever state it happened to find, which silently produced half-logged-in
    profiles.
    """
    settings = settings or get_settings()
    from playwright.async_api import async_playwright

    profile_dir = Path(settings.browser_profile_dir) / source
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser_type = getattr(playwright, settings.browser)
        context = await browser_type.launch_persistent_context(
            str(profile_dir),
            headless=False,  # the whole point is that the user can see it
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        print(f"\n  A browser window is open for {source}.")
        print("  Log in there — including any OTP or MFA step.")
        print("  Close the window when you are done; the session is saved.\n")

        closed = False
        try:
            # Wait for the user to close the window.
            await context.wait_for_event("close", timeout=0)
            closed = True
        except Exception:  # noqa: BLE001 - a closed context raises on some builds
            closed = True
        finally:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass

    return {
        "source": source,
        "profile": str(profile_dir),
        "completed": closed,
        "note": "The session lives in the browser profile and survives restarts.",
    }
