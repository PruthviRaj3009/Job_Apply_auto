"""
Analysis service: take NEW jobs through JD analysis and matching.

Drives the NEW -> ANALYZING -> MATCHED/REJECTED leg of the pipeline. One job
failing is recorded and skipped; it never stops the batch (section 30).
"""

from __future__ import annotations

import logging
import traceback

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analysis import parse_jd
from ..config import Settings, get_settings
from ..db.events import log_event, move_job
from ..matching import MatchingEngine, ProfileSnapshot
from ..models import (
    ApplicationError,
    EventType,
    Job,
    JobStatus,
    Profile,
)

logger = logging.getLogger(__name__)


class AnalysisService:
    """
    Analyses jobs against the master profile.

    The profile snapshot is built once per service instance, not once per job:
    a 200-job batch would otherwise walk the same ORM relationships 200 times.
    """

    def __init__(
        self,
        session: Session,
        profile: Profile,
        *,
        settings: Settings | None = None,
        semantic: object | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.snapshot = ProfileSnapshot.from_profile(profile)
        self.engine = MatchingEngine(self.snapshot, semantic=semantic)

    # ------------------------------------------------------------ single

    def analyze_job(self, job: Job) -> Job:
        """
        Analyse and score one job, moving it to MATCHED or REJECTED.

        Raises nothing for ordinary data problems — a job with no description
        is still scorable on its title, with a concern recorded to say so.
        """
        move_job(self.session, job, JobStatus.ANALYZING)

        parsed = parse_jd(job.description or "")
        for key, value in parsed.as_job_fields().items():
            # Keep whatever the source already gave us and add what the JD
            # yielded; a source's own skill tags are often better than prose.
            if key == "skills":
                merged = list(job.skills or [])
                merged += [s for s in value if s not in merged]
                job.skills = merged
            elif value:
                setattr(job, key, value)

        result = self.engine.match(job, parsed)
        job.match_score = result.match_score
        job.match_detail = result.to_dict()

        log_event(
            self.session,
            EventType.JOB_ANALYZED,
            job_id=job.id,
            message=result.match_explanation,
            payload={"score": result.match_score, "signals": result.signals_used},
        )

        if result.match_score >= self.settings.min_match_score:
            move_job(self.session, job, JobStatus.MATCHED, note=result.match_explanation)
        else:
            move_job(
                self.session,
                job,
                JobStatus.REJECTED,
                note=(
                    f"Score {result.match_score:.2f} below threshold "
                    f"{self.settings.min_match_score:.2f}. {result.match_explanation}"
                ),
            )
        return job

    # ------------------------------------------------------------- batch

    def analyze_pending(self, limit: int = 50) -> dict[str, int]:
        """
        Analyse everything waiting.

        Returns counts rather than jobs: callers want a summary, and holding
        a large result set alive across the batch serves nobody.
        """
        jobs = list(
            self.session.scalars(
                select(Job)
                .where(Job.status.in_([JobStatus.NEW, JobStatus.SEEN]))
                .order_by(Job.first_seen_at.asc())
                .limit(limit)
            )
        )

        counts = {"analyzed": 0, "matched": 0, "rejected": 0, "failed": 0}
        for job in jobs:
            try:
                self.analyze_job(job)
            except Exception as exc:  # noqa: BLE001 - isolate per job
                logger.exception("Analysis failed for job %s", job.id)
                counts["failed"] += 1
                self._record_failure(job, exc)
                continue

            counts["analyzed"] += 1
            if job.status == JobStatus.MATCHED:
                counts["matched"] += 1
            elif job.status == JobStatus.REJECTED:
                counts["rejected"] += 1

        return counts

    def _record_failure(self, job: Job, exc: Exception) -> None:
        """
        Park a failed job in FAILED with the error stored.

        Leaving it in ANALYZING would strand it: the next batch selects on NEW
        and SEEN, so the job would never be looked at again.
        """
        self.session.add(
            ApplicationError(
                job_id=job.id,
                stage="analysis",
                error_type=type(exc).__name__,
                message=str(exc),
                traceback=traceback.format_exc(),
            )
        )
        try:
            move_job(self.session, job, JobStatus.FAILED, note=f"Analysis error: {exc}")
        except Exception:  # noqa: BLE001 - never mask the original failure
            logger.warning("Could not move job %s to FAILED", job.id)

    # ----------------------------------------------------------- queries

    def matched_jobs(self, limit: int = 50) -> list[Job]:
        """Matched jobs, best score first — apply to the best ones first."""
        return list(
            self.session.scalars(
                select(Job)
                .where(Job.status == JobStatus.MATCHED)
                .order_by(Job.match_score.desc())
                .limit(limit)
            )
        )
