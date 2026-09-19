"""Scheduler: restartability and query construction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.models import Job, JobStatus
from app.scheduler import STUCK_AFTER, ScheduleConfig, build_query, recover_stuck

from .conftest import make_job


def _age(session, job, minutes: int) -> None:
    """
    Backdate updated_at.

    Written through a Core UPDATE rather than by assignment because the column
    has an onupdate hook — assigning to it would be overwritten on flush.
    """
    session.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes))
    )
    session.commit()


def test_interrupted_analysis_is_recovered(session, settings):
    """
    Section 15: processing must be restartable. Without this the job is
    stranded — the analysis query only selects NEW and SEEN.
    """
    job = make_job(session, fingerprint="stuck-analyze")
    job.status = JobStatus.ANALYZING
    session.flush()
    _age(session, job, minutes=60)

    assert recover_stuck(settings) == 1
    session.expire_all()
    assert session.get(Job, job.id).status is JobStatus.SEEN


def test_interrupted_application_becomes_retry_pending(session, settings):
    """
    An explicit "this was interrupted" state rather than a silent re-run, so
    the user can see it happened.
    """
    job = make_job(session, fingerprint="stuck-apply")
    job.status = JobStatus.APPLYING
    session.flush()
    _age(session, job, minutes=60)

    assert recover_stuck(settings) == 1
    session.expire_all()
    assert session.get(Job, job.id).status is JobStatus.RETRY_PENDING


def test_a_job_a_live_worker_is_holding_is_not_yanked(session, settings):
    """
    Only jobs untouched for the grace period are recovered, so an in-flight
    application is left alone.
    """
    job = make_job(session, fingerprint="in-flight")
    job.status = JobStatus.APPLYING
    session.flush()
    _age(session, job, minutes=int(STUCK_AFTER.total_seconds() // 60) - 5)

    assert recover_stuck(settings) == 0
    session.expire_all()
    assert session.get(Job, job.id).status is JobStatus.APPLYING


def test_settled_jobs_are_untouched(session, settings):
    for status in (JobStatus.SUBMITTED, JobStatus.REJECTED, JobStatus.MATCHED, JobStatus.NEW):
        job = make_job(session, fingerprint=f"settled-{status}")
        job.status = status
        session.flush()
        _age(session, job, minutes=120)

    assert recover_stuck(settings) == 0


def test_recovery_leaves_a_trace(session, settings):
    """A status change nobody can see is a status change nobody can debug."""
    from app.models import ApplicationEvent

    job = make_job(session, fingerprint="traced")
    job.status = JobStatus.ANALYZING
    session.flush()
    _age(session, job, minutes=60)
    recover_stuck(settings)

    session.expire_all()
    events = session.query(ApplicationEvent).filter_by(job_id=job.id).all()
    assert any("interrupted" in (e.message or "") for e in events)


def test_query_is_built_from_the_profile(session, profile, settings):
    """
    Changing what is searched should be an edit to the profile, not to code.
    """
    profile.search_keywords = ["Platform Engineer"]
    profile.excluded_keywords = ["helpdesk"]
    profile.excluded_companies = ["Globex"]
    profile.preferences = {"remote_only": True, "max_days_old": 7}
    session.flush()

    query = build_query(profile, settings)
    assert query.keywords == ["Platform Engineer"]
    assert query.remote_only is True
    assert query.max_days_old == 7
    assert query.excluded_keywords == ["helpdesk"]
    assert query.experience_years == profile.total_experience_years


def test_query_falls_back_to_target_roles(session, profile, settings):
    """A profile with roles but no explicit keywords still searches."""
    profile.search_keywords = []
    session.flush()
    assert build_query(profile, settings).keywords == profile.target_roles


def test_schedule_defaults_match_the_specified_example(settings):
    """Section 20: discovery every 30 minutes, processing every hour."""
    config = ScheduleConfig()
    assert config.discovery_interval == 30 * 60
    assert config.processing_interval == 60 * 60
