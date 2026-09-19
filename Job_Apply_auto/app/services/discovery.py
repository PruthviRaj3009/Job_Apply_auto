"""
Discovery service: run sources, normalize, deduplicate, persist.

This is where "detect newly posted jobs" (section 4) actually happens. A job
is *new* when its fingerprint has never been stored. Everything else is a
re-sighting, which updates the existing row's freshness without resetting its
status — a job already rejected must not silently return to NEW and get
re-analysed on every poll.

One failing source never stops the run (section 30): each adapter is isolated,
its failure recorded, and the rest continue.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.events import log_event
from ..models import EventType, Job, JobSource, JobStatus
from ..sources import (
    JobSourceAdapter,
    RawJob,
    SearchQuery,
    apply_filters,
    get_adapter,
    normalize,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SourceOutcome:
    """What one adapter did on one run."""

    slug: str
    found: int = 0
    kept: int = 0
    new: int = 0
    updated: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(slots=True)
class DiscoveryReport:
    """Aggregate result, shaped for the dashboard and the CLI."""

    outcomes: list[SourceOutcome] = field(default_factory=list)
    new_job_ids: list[int] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_found(self) -> int:
        return sum(o.found for o in self.outcomes)

    @property
    def total_new(self) -> int:
        return len(self.new_job_ids)

    @property
    def failed_sources(self) -> list[str]:
        return [o.slug for o in self.outcomes if not o.ok]

    def summary(self) -> str:
        parts = [
            f"{self.total_found} found",
            f"{self.total_new} new",
            f"{len(self.dropped)} filtered out",
        ]
        if self.failed_sources:
            parts.append(f"{len(self.failed_sources)} source(s) failed")
        return ", ".join(parts)


class DiscoveryService:
    """
    Runs adapters and writes results into the jobs table.

    Persistence is deliberately synchronous while fetching is async: a browser
    page cannot be shared across threads, and holding a DB transaction open
    across a multi-minute scrape would block every other writer.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------ persistence

    def upsert(self, raw: RawJob, *, source: JobSource | None = None) -> tuple[Job, bool]:
        """
        Store a discovered job. Returns (job, is_new).

        A re-sighting refreshes the fields a source may have improved (a
        description filled in later, an application URL that only appears once
        logged in) but never touches `status`. Resetting status here would
        undo matching and re-apply to jobs already handled.
        """
        fields = normalize(raw)
        fingerprint = fields["fingerprint"]

        existing = self.session.scalar(select(Job).where(Job.fingerprint == fingerprint))
        if existing is not None:
            self._refresh(existing, fields)
            return existing, False

        job = Job(**fields)
        job.status = JobStatus.NEW
        job.first_seen_at = datetime.now(timezone.utc)
        if source is not None:
            job.source_id = source.id
        self.session.add(job)
        self.session.flush()

        log_event(
            self.session,
            EventType.JOB_DISCOVERED,
            job_id=job.id,
            message=f"{job.title} @ {job.company}",
            payload={"source": raw.source_slug, "url": job.url},
        )
        return job, True

    @staticmethod
    def _refresh(job: Job, fields: dict[str, Any]) -> None:
        """
        Fill gaps on an already-known job without overwriting good data.

        Only empty fields are populated: a listing page that omits the salary
        must not blank out a salary a detail page already supplied.
        """
        for key in (
            "description",
            "salary_text",
            "application_url",
            "posted_at",
            "salary_min",
            "salary_max",
            "experience_min_years",
            "experience_max_years",
        ):
            if getattr(job, key, None) in (None, "") and fields.get(key) not in (None, ""):
                setattr(job, key, fields[key])
        if not job.skills and fields.get("skills"):
            job.skills = fields["skills"]
        # easy_apply can only improve: a source that now offers a native flow
        # is news; one that failed to detect it is not.
        if fields.get("easy_apply") and not job.easy_apply:
            job.easy_apply = True

    def ensure_source(self, adapter: JobSourceAdapter) -> JobSource:
        """Get or create the JobSource row backing an adapter."""
        source = self.session.scalar(select(JobSource).where(JobSource.slug == adapter.slug))
        if source is None:
            source = JobSource(
                slug=adapter.slug,
                display_name=adapter.display_name or adapter.slug,
                kind=adapter.kind,
            )
            self.session.add(source)
            self.session.flush()
        return source

    def record_raw_jobs(
        self,
        raws: Sequence[RawJob],
        query: SearchQuery,
        adapter: JobSourceAdapter,
    ) -> SourceOutcome:
        """
        Filter and persist one adapter's results.

        Split out from the async fetch so it can be tested with hand-built
        RawJobs and no browser at all.
        """
        outcome = SourceOutcome(slug=adapter.slug, found=len(raws))
        kept, dropped = apply_filters(raws, query)
        outcome.kept = len(kept)

        source = self.ensure_source(adapter)
        source.last_polled_at = datetime.now(timezone.utc)

        for raw in kept:
            try:
                _, is_new = self.upsert(raw, source=source)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the batch
                logger.warning("Failed to store %s from %s: %s", raw.url, adapter.slug, exc)
                continue
            if is_new:
                outcome.new += 1
            else:
                outcome.updated += 1

        self._last_dropped = [(d.title, reason) for d, reason in dropped]
        return outcome

    # ----------------------------------------------------------------- async

    async def discover(
        self,
        page_factory: Any,
        query: SearchQuery,
        source_slugs: Sequence[str],
    ) -> DiscoveryReport:
        """
        Run each named source and persist what it finds.

        `page_factory` is an async callable returning a browser page for a
        given source slug — injected rather than constructed here so tests can
        pass a stub and the browser layer stays replaceable.
        """
        report = DiscoveryReport()

        for slug in source_slugs:
            try:
                adapter = get_adapter(slug)
            except KeyError as exc:
                report.outcomes.append(SourceOutcome(slug=slug, error=str(exc)))
                continue

            try:
                async with page_factory(slug) as page:
                    raws = await adapter.search_jobs(page, query)
            except Exception as exc:  # noqa: BLE001 - isolate every source
                logger.exception("Source %s failed", slug)
                report.outcomes.append(SourceOutcome(slug=slug, error=f"{type(exc).__name__}: {exc}"))
                continue

            outcome = self.record_raw_jobs(raws, query, adapter)
            report.outcomes.append(outcome)
            report.dropped.extend(getattr(self, "_last_dropped", []))

            # Politeness between sources, on top of any in-adapter pacing.
            await asyncio.sleep(adapter.request_delay)

        report.new_job_ids = [
            job_id
            for (job_id,) in self.session.execute(
                select(Job.id).where(Job.status == JobStatus.NEW)
            ).all()
        ]
        return report

    # ------------------------------------------------------------- queries

    def new_jobs(self, limit: int = 100) -> list[Job]:
        """Jobs awaiting analysis, oldest first so nothing starves."""
        return list(
            self.session.scalars(
                select(Job)
                .where(Job.status == JobStatus.NEW)
                .order_by(Job.first_seen_at.asc())
                .limit(limit)
            )
        )
