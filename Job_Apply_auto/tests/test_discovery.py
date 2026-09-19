"""Filtering and discovery persistence: dedup, re-sighting, isolation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import ApplicationEvent, EventType, Job, JobSource, JobStatus
from app.models.enums import EmploymentType, WorkMode
from app.services.discovery import DiscoveryService
from app.sources import RawJob, SearchQuery, apply_filters, evaluate, get_adapter

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def raw(**kwargs) -> RawJob:
    base = dict(
        title="DevOps Engineer",
        url="https://naukri.com/job/1",
        source_slug="naukri",
        company="Acme",
        location="Pune",
    )
    base.update(kwargs)
    return RawJob(**base)


# ---------------------------------------------------------------- filters

def test_excluded_keyword_drops_a_job():
    q = SearchQuery(excluded_keywords=["helpdesk"])
    assert not evaluate(raw(title="IT Helpdesk Engineer"), q, now=NOW)


def test_excluded_company_matches_legal_suffixes():
    """Blocking "Acme" must also block "Acme Technologies Pvt Ltd"."""
    q = SearchQuery(excluded_companies=["Acme"])
    assert not evaluate(raw(company="Acme Technologies Pvt Ltd"), q, now=NOW)


def test_excluded_company_does_not_over_match():
    q = SearchQuery(excluded_companies=["Acme"])
    assert evaluate(raw(company="Globex"), q, now=NOW)


def test_stale_job_is_dropped():
    q = SearchQuery(max_days_old=7)
    assert not evaluate(raw(posted_days_ago=30), q, now=NOW)


def test_fresh_job_is_kept():
    assert evaluate(raw(posted_days_ago=2), SearchQuery(max_days_old=7), now=NOW)


def test_unknown_posting_date_is_not_a_rejection():
    """
    Most portals do not publish a date on the listing page. Treating that as
    "too old" would discard nearly everything.
    """
    assert evaluate(raw(posted_days_ago=None), SearchQuery(max_days_old=7), now=NOW)


def test_undisclosed_salary_is_not_below_minimum():
    """
    Indian portals usually hide salary. An unknown salary must not be read as
    a failing one — that would filter out most real listings.
    """
    q = SearchQuery(salary_min=1_000_000)
    assert evaluate(raw(salary_text="Not disclosed"), q, now=NOW)


def test_salary_below_minimum_is_dropped():
    q = SearchQuery(salary_min=2_000_000)
    assert not evaluate(raw(salary_text="8-12 LPA"), q, now=NOW)


def test_experience_overshoot_within_one_year_is_tolerated():
    """A 4-year candidate is a reasonable applicant for a "5+ years" post."""
    q = SearchQuery(experience_years=4)
    assert evaluate(raw(experience_text="5+ years"), q, now=NOW)
    assert not evaluate(raw(experience_text="10+ years"), q, now=NOW)


def test_remote_only_keeps_unknown_work_mode():
    """
    Filtering out UNKNOWN would discard listings whose page simply did not say
    — the description fetched later often reveals it is remote.
    """
    q = SearchQuery(remote_only=True)
    assert evaluate(raw(work_mode=WorkMode.UNKNOWN), q, now=NOW)
    assert evaluate(raw(work_mode=WorkMode.REMOTE), q, now=NOW)
    assert not evaluate(raw(work_mode=WorkMode.ONSITE), q, now=NOW)


def test_easy_apply_filter():
    q = SearchQuery(easy_apply_only=True)
    assert evaluate(raw(easy_apply=True), q, now=NOW)
    assert not evaluate(raw(easy_apply=False), q, now=NOW)


def test_employment_type_filter_ignores_unknown():
    q = SearchQuery(employment_types=[EmploymentType.FULL_TIME])
    assert evaluate(raw(employment_type=EmploymentType.UNKNOWN), q, now=NOW)
    assert not evaluate(raw(employment_type=EmploymentType.INTERNSHIP), q, now=NOW)


def test_unusable_job_is_dropped():
    assert not evaluate(raw(title=""), SearchQuery(), now=NOW)
    assert not evaluate(raw(url=""), SearchQuery(), now=NOW)


def test_apply_filters_reports_reasons():
    """A silently vanishing job is the hardest kind of bug to notice."""
    jobs = [raw(title="DevOps Engineer"), raw(title="Helpdesk Technician", url="https://x/2")]
    kept, dropped = apply_filters(jobs, SearchQuery(excluded_keywords=["helpdesk"]), now=NOW)

    assert len(kept) == 1
    assert len(dropped) == 1
    assert "helpdesk" in dropped[0][1]


# ------------------------------------------------------------- persistence

def test_first_sighting_creates_a_new_job(session):
    svc = DiscoveryService(session)
    job, is_new = svc.upsert(raw())
    session.flush()

    assert is_new is True
    assert job.status == JobStatus.NEW
    assert job.first_seen_at is not None


def test_same_job_from_two_portals_is_stored_once(session):
    """
    The cross-posting case: same role, different portal, different URL and
    spelling. One row, not two applications.
    """
    svc = DiscoveryService(session)
    svc.upsert(raw(company="Acme Technologies Pvt Ltd", title="Senior DevOps Engineer",
                   location="Pune, Maharashtra", url="https://naukri.com/a"))
    session.flush()
    _, is_new = svc.upsert(raw(source_slug="linkedin", company="ACME Technologies",
                               title="DevOps Engineer", location="Pune",
                               url="https://linkedin.com/jobs/view/b"))
    session.flush()

    assert is_new is False
    assert session.query(Job).count() == 1


def test_resighting_does_not_reset_status(session):
    """
    A job already rejected must not return to NEW on the next poll and get
    re-analysed forever.
    """
    svc = DiscoveryService(session)
    job, _ = svc.upsert(raw())
    job.status = JobStatus.REJECTED
    session.flush()

    svc.upsert(raw())
    session.flush()
    assert job.status == JobStatus.REJECTED


def test_resighting_fills_gaps_without_overwriting(session):
    """
    A listing page that omits the salary must not blank out one a detail page
    already supplied.
    """
    svc = DiscoveryService(session)
    job, _ = svc.upsert(raw(salary_text="12-18 LPA", description="Full description"))
    session.flush()

    svc.upsert(raw(salary_text="", description=""))
    session.flush()

    assert job.salary_text == "12-18 LPA"
    assert job.description == "Full description"


def test_resighting_adds_newly_available_detail(session):
    svc = DiscoveryService(session)
    job, _ = svc.upsert(raw(description=""))
    session.flush()
    assert job.description is None

    svc.upsert(raw(description="Now we have the JD"))
    session.flush()
    assert job.description == "Now we have the JD"


def test_easy_apply_only_improves(session):
    """Detecting a native flow is news; failing to detect one is not."""
    svc = DiscoveryService(session)
    job, _ = svc.upsert(raw(easy_apply=True))
    session.flush()

    svc.upsert(raw(easy_apply=False))
    session.flush()
    assert job.easy_apply is True


def test_discovery_logs_a_job_discovered_event(session):
    svc = DiscoveryService(session)
    job, _ = svc.upsert(raw())
    session.flush()

    events = session.scalars(
        select(ApplicationEvent).where(ApplicationEvent.job_id == job.id)
    ).all()
    assert [e.event_type for e in events] == [EventType.JOB_DISCOVERED]


def test_record_raw_jobs_filters_then_persists(session):
    svc = DiscoveryService(session)
    adapter = get_adapter("naukri")
    jobs = [
        raw(title="DevOps Engineer", url="https://n/1"),
        raw(title="Helpdesk Technician", company="Globex", url="https://n/2"),
    ]
    outcome = svc.record_raw_jobs(jobs, SearchQuery(excluded_keywords=["helpdesk"]), adapter)
    session.flush()

    assert outcome.found == 2
    assert outcome.kept == 1
    assert outcome.new == 1
    assert session.query(Job).count() == 1


def test_record_raw_jobs_creates_the_source_row(session):
    svc = DiscoveryService(session)
    svc.record_raw_jobs([raw()], SearchQuery(), get_adapter("naukri"))
    session.flush()

    source = session.scalar(select(JobSource).where(JobSource.slug == "naukri"))
    assert source is not None
    assert source.last_polled_at is not None


def test_new_jobs_returns_oldest_first(session):
    """Oldest-first ordering keeps a backlog from starving."""
    svc = DiscoveryService(session)
    older, _ = svc.upsert(raw(url="https://n/1", title="Older Role"))
    older.first_seen_at = NOW - timedelta(days=2)
    newer, _ = svc.upsert(raw(url="https://n/2", title="Newer Role"))
    newer.first_seen_at = NOW
    session.flush()

    assert [j.id for j in svc.new_jobs()] == [older.id, newer.id]
