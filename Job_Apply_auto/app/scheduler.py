"""
Scheduler and pipeline orchestration (section 20).

Two loops on independent intervals, as the section describes: discovery every
30 minutes by default, processing every hour.

The property that matters most here is **restartability** (section 15). A
crashed process leaves jobs parked in ANALYZING or APPLYING; `recover_stuck`
sweeps those back into a runnable state on startup, so a crash costs one
iteration rather than stranding jobs forever.

No work is ever done twice: every stage selects on status, and every stage
moves the job out of that status before it finishes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .browser import BrowserManager
from .config import Settings, get_settings
from .db import session_scope
from .db.events import move_job
from .engine import ApplicationEngine
from .models import ApplicationEvent, EventType, Job, JobStatus, Profile
from .services.analysis import AnalysisService
from .services.discovery import DiscoveryService
from .services.resume_service import ResumeService
from .sources import SearchQuery, available_sources

logger = logging.getLogger(__name__)

#: How long a job may sit in a working state before it is presumed abandoned.
STUCK_AFTER = timedelta(minutes=30)


@dataclass(slots=True)
class ScheduleConfig:
    """Intervals, in seconds. Section 20's example is the default."""

    discovery_interval: int = 30 * 60
    processing_interval: int = 60 * 60
    sources: list[str] = field(default_factory=list)
    enabled: bool = True


def build_query(profile: Profile, settings: Settings) -> SearchQuery:
    """
    Turn the user's profile and preferences into a search (section 5).

    Everything comes from the profile, so changing what is searched is an
    edit to the profile rather than a code change.
    """
    preferences = profile.preferences or {}
    return SearchQuery(
        keywords=profile.search_keywords or profile.target_roles or [],
        location=(profile.preferred_locations or [""])[0],
        remote_only=bool(preferences.get("remote_only", False)),
        experience_years=profile.total_experience_years,
        max_days_old=int(preferences.get("max_days_old", 30)),
        easy_apply_only=bool(preferences.get("easy_apply_only", False)),
        excluded_keywords=profile.excluded_keywords or [],
        excluded_companies=profile.excluded_companies or [],
        limit=int(preferences.get("discovery_limit", 100)),
    )


def recover_stuck(settings: Settings | None = None) -> int:
    """
    Return abandoned jobs to a runnable state.

    A process killed mid-analysis leaves a job in ANALYZING, and the analysis
    query only selects NEW and SEEN — so without this the job is stranded
    permanently. Only jobs untouched for STUCK_AFTER are moved, so a job a
    live worker is currently holding is not yanked out from under it.
    """
    settings = settings or get_settings()
    cutoff = datetime.now(timezone.utc) - STUCK_AFTER
    recovered = 0

    with session_scope(settings) as session:
        stuck = session.scalars(
            select(Job).where(
                Job.status.in_([JobStatus.ANALYZING, JobStatus.APPLYING]),
                Job.updated_at < cutoff,
            )
        )
        for job in stuck:
            previous = JobStatus(job.status)
            # ANALYZING rewinds to SEEN so analysis picks it up again;
            # APPLYING becomes RETRY_PENDING, which is an explicit "this was
            # interrupted" state rather than a silent re-run.
            target = JobStatus.SEEN if previous is JobStatus.ANALYZING else JobStatus.RETRY_PENDING
            if previous is JobStatus.ANALYZING:
                # ANALYZING has no legal edge back to SEEN — it is a rewind,
                # not a transition, so it is written directly and logged.
                job.status = JobStatus.SEEN
                job.status_note = "Recovered after an interrupted analysis"
                session.add(
                    ApplicationEvent(
                        job_id=job.id,
                        event_type=EventType.RETRY_STARTED,
                        message="Recovered from interrupted analysis",
                        created_at=datetime.now(timezone.utc),
                    )
                )
            else:
                move_job(session, job, target, note="Recovered after an interrupted application")
            recovered += 1
            logger.info("Recovered job %s from %s", job.id, previous)

    return recovered


class Pipeline:
    """
    Runs the stages. Each is independently callable, so the CLI can run one
    without the scheduler and the scheduler is just a timer over them.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _profile(self, session) -> Profile | None:
        return session.scalar(select(Profile).order_by(Profile.id))

    # -------------------------------------------------------- discovery

    async def discover(self, sources: list[str] | None = None) -> dict:
        """Run discovery across the requested sources."""
        sources = sources or available_sources()

        with session_scope(self.settings) as session:
            profile = self._profile(session)
            if profile is None:
                return {"error": "No profile configured."}
            query = build_query(profile, self.settings)

            async with BrowserManager(self.settings) as browser:
                service = DiscoveryService(session)
                report = await service.discover(browser.page, query, sources)

            logger.info("Discovery: %s", report.summary())
            return {
                "summary": report.summary(),
                "found": report.total_found,
                "new": report.total_new,
                "failed_sources": report.failed_sources,
            }

    # --------------------------------------------------------- analysis

    def analyze(self, limit: int = 50) -> dict:
        """Score everything waiting. Synchronous — no browser involved."""
        with session_scope(self.settings) as session:
            profile = self._profile(session)
            if profile is None:
                return {"error": "No profile configured."}
            counts = AnalysisService(session, profile, settings=self.settings).analyze_pending(limit)
            logger.info("Analysis: %s", counts)
            return counts

    # ------------------------------------------------------- applying

    async def apply_to_matched(self, limit: int | None = None) -> dict:
        """
        Prepare (and, if enabled, submit) applications for matched jobs.

        Preparation always runs: it produces the resume and surfaces the
        questions, which is useful even in dry-run. Submission is gated
        inside the engine.
        """
        limit = limit or self.settings.max_applications_per_run
        results = {"prepared": 0, "submitted": 0, "waiting": 0, "failed": 0}

        with session_scope(self.settings) as session:
            profile = self._profile(session)
            if profile is None:
                return {"error": "No profile configured."}

            jobs = list(
                session.scalars(
                    select(Job)
                    .where(Job.status.in_([JobStatus.MATCHED, JobStatus.RETRY_PENDING]))
                    .order_by(Job.match_score.desc())
                    .limit(limit)
                )
            )
            if not jobs:
                return results

            engine = ApplicationEngine(
                session,
                profile,
                settings=self.settings,
                resume_service=ResumeService(session, profile, settings=self.settings),
            )

            for job in jobs:
                try:
                    prepared = engine.prepare_application(job)
                    results["prepared"] += 1
                    if prepared.outstanding_questions:
                        results["waiting"] += 1
                        # Leave it waiting; the user has questions to answer.
                        await engine.submit_application(prepared, None)
                        continue
                    outcome = await engine.submit_application(prepared, None)
                    if outcome.submitted:
                        results["submitted"] += 1
                except Exception:  # noqa: BLE001 - isolate per job (section 30)
                    logger.exception("Application failed for job %s", job.id)
                    results["failed"] += 1

        logger.info("Applications: %s", results)
        return results

    # ------------------------------------------------------ full cycle

    async def process_once(self) -> dict:
        """One full pass: analyse, then apply."""
        return {
            "analysis": self.analyze(),
            "applications": await self.apply_to_matched(),
        }


class Scheduler:
    """
    Two independent loops on their own intervals.

    Independent because their costs differ by an order of magnitude:
    discovery drives a browser across eight portals, processing is mostly
    local compute. Tying them to one interval would mean either hammering the
    portals or leaving matched jobs sitting.
    """

    def __init__(self, config: ScheduleConfig | None = None, settings: Settings | None = None) -> None:
        self.config = config or ScheduleConfig()
        self.settings = settings or get_settings()
        self.pipeline = Pipeline(self.settings)
        self._stopping = asyncio.Event()

    async def _loop(self, name: str, interval: int, work) -> None:
        while not self._stopping.is_set():
            try:
                await work()
            except Exception:  # noqa: BLE001 - a loop must never die
                logger.exception("%s cycle failed; continuing", name)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def run(self) -> None:
        """Start both loops. Runs until `stop()`."""
        recovered = recover_stuck(self.settings)
        if recovered:
            logger.info("Recovered %d interrupted job(s) on startup", recovered)

        logger.info(
            "Scheduler running: discovery every %ds, processing every %ds",
            self.config.discovery_interval,
            self.config.processing_interval,
        )
        await asyncio.gather(
            self._loop(
                "discovery",
                self.config.discovery_interval,
                lambda: self.pipeline.discover(self.config.sources or None),
            ),
            self._loop(
                "processing",
                self.config.processing_interval,
                self.pipeline.process_once,
            ),
        )

    def stop(self) -> None:
        self._stopping.set()
