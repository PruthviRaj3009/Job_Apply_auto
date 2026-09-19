"""
Dashboard and analytics aggregation (section 19).

Pure read-side. Every figure here is derived from the database, which is the
source of truth — nothing is cached or recomputed from the sheet.

The counts are deliberately the ones section 19 names, because those are what
tell a user whether the pipeline is working: a run producing 200 discovered
jobs and 0 matches is broken in a way "200 jobs found" alone would hide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Application,
    ApplicationStatus,
    Job,
    JobStatus,
    PendingQuestion,
    ResumeVersion,
)


@dataclass(slots=True)
class DashboardCounts:
    """The headline numbers (section 19)."""

    total_jobs: int = 0
    new_jobs: int = 0
    matched_jobs: int = 0
    rejected_jobs: int = 0
    applications_prepared: int = 0
    submitted: int = 0
    failed: int = 0
    waiting_for_user: int = 0
    interview_invitations: int = 0
    rejections: int = 0
    #: Distinct from rejected_jobs: questions the user has to answer before
    #: anything else can happen. This is the number that should drive action.
    action_required: int = 0

    def to_dict(self) -> dict:
        return {
            "total_jobs": self.total_jobs,
            "new_jobs": self.new_jobs,
            "matched_jobs": self.matched_jobs,
            "rejected_jobs": self.rejected_jobs,
            "applications_prepared": self.applications_prepared,
            "submitted": self.submitted,
            "failed": self.failed,
            "waiting_for_user": self.waiting_for_user,
            "interview_invitations": self.interview_invitations,
            "rejections": self.rejections,
            "action_required": self.action_required,
        }


@dataclass(slots=True)
class Analytics:
    counts: DashboardCounts = field(default_factory=DashboardCounts)
    by_source: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    by_company: dict[str, int] = field(default_factory=dict)
    applications_per_day: dict[str, int] = field(default_factory=dict)
    match_score_distribution: dict[str, int] = field(default_factory=dict)
    response_rate: float = 0.0
    interview_rate: float = 0.0
    top_missing_skills: list[tuple[str, int]] = field(default_factory=list)
    resume_versions: int = 0

    def to_dict(self) -> dict:
        return {
            "counts": self.counts.to_dict(),
            "by_source": self.by_source,
            "by_status": self.by_status,
            "by_company": self.by_company,
            "applications_per_day": self.applications_per_day,
            "match_score_distribution": self.match_score_distribution,
            "response_rate": round(self.response_rate, 3),
            "interview_rate": round(self.interview_rate, 3),
            "top_missing_skills": self.top_missing_skills,
            "resume_versions": self.resume_versions,
        }


class AnalyticsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _count_jobs(self, status: JobStatus) -> int:
        return self.session.scalar(
            select(func.count()).select_from(Job).where(Job.status == status)
        ) or 0

    def _count_applications(self, status: ApplicationStatus) -> int:
        return self.session.scalar(
            select(func.count()).select_from(Application).where(Application.status == status)
        ) or 0

    def counts(self) -> DashboardCounts:
        return DashboardCounts(
            total_jobs=self.session.scalar(select(func.count()).select_from(Job)) or 0,
            new_jobs=self._count_jobs(JobStatus.NEW),
            matched_jobs=self._count_jobs(JobStatus.MATCHED),
            rejected_jobs=self._count_jobs(JobStatus.REJECTED),
            applications_prepared=self._count_applications(ApplicationStatus.PREPARED),
            submitted=self._count_applications(ApplicationStatus.SUBMITTED),
            failed=self._count_applications(ApplicationStatus.FAILED),
            waiting_for_user=self._count_jobs(JobStatus.WAITING_FOR_USER),
            interview_invitations=self._count_applications(ApplicationStatus.INTERVIEW),
            rejections=self._count_applications(ApplicationStatus.REJECTED),
            action_required=self.session.scalar(
                select(func.count())
                .select_from(PendingQuestion)
                .where(PendingQuestion.resolved.is_(False))
            ) or 0,
        )

    def analytics(self, *, days: int = 30) -> Analytics:
        result = Analytics(counts=self.counts())
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        for source, count in self.session.execute(
            select(Application.source, func.count()).group_by(Application.source)
        ):
            result.by_source[source or "unknown"] = count

        for status, count in self.session.execute(
            select(Application.status, func.count()).group_by(Application.status)
        ):
            result.by_status[str(status)] = count

        for company, count in self.session.execute(
            select(Application.company, func.count())
            .group_by(Application.company)
            .order_by(func.count().desc())
            .limit(20)
        ):
            result.by_company[company or "unknown"] = count

        for application in self.session.scalars(
            select(Application).where(Application.created_at >= cutoff)
        ):
            day = application.created_at.strftime("%Y-%m-%d")
            result.applications_per_day[day] = result.applications_per_day.get(day, 0) + 1

        # Score buckets, so a histogram can be drawn without shipping every
        # individual score to the client.
        buckets = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
        for (score,) in self.session.execute(
            select(Job.match_score).where(Job.match_score.isnot(None))
        ):
            index = min(int(score * 5), 4)
            buckets[list(buckets)[index]] += 1
        result.match_score_distribution = buckets

        submitted = self._count_applications(ApplicationStatus.SUBMITTED)
        responded = sum(
            self._count_applications(s)
            for s in (
                ApplicationStatus.VIEWED,
                ApplicationStatus.RESPONDED,
                ApplicationStatus.INTERVIEW,
                ApplicationStatus.OFFER,
                ApplicationStatus.REJECTED,
            )
        )
        interviews = self._count_applications(ApplicationStatus.INTERVIEW) + \
            self._count_applications(ApplicationStatus.OFFER)

        # Rates are over everything that actually went out, including the
        # applications that have since moved on from SUBMITTED.
        total_sent = submitted + responded
        if total_sent:
            result.response_rate = responded / total_sent
            result.interview_rate = interviews / total_sent

        # What the user keeps being rejected for — the most actionable number
        # on the whole dashboard, because it says what to learn next.
        missing: dict[str, int] = {}
        for (detail,) in self.session.execute(
            select(Job.match_detail).where(Job.match_detail.isnot(None))
        ):
            for skill in (detail or {}).get("missing_skills") or []:
                missing[skill] = missing.get(skill, 0) + 1
        result.top_missing_skills = sorted(missing.items(), key=lambda kv: -kv[1])[:15]

        result.resume_versions = self.session.scalar(
            select(func.count()).select_from(ResumeVersion)
        ) or 0

        return result
