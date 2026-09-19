"""
End-to-end: discovery through to a prepared application.

Section 28's acceptance workflow, exercised in one pass with a stub source
and no browser. This is the test that would catch a wiring break between
subsystems that each pass their own tests.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app.engine import ApplicationEngine, DetectedQuestion
from app.models import Application, ApplicationStatus, Job, JobStatus, ResumeVersion
from app.qa import KnowledgeBase
from app.services.analysis import AnalysisService
from app.services.discovery import DiscoveryService
from app.services.profile_loader import ProfileFormatError, load_profile
from app.services.resume_service import ResumeService
from app.sources import RawJob, SearchQuery, get_adapter

PROFILE_JSON = {
    "full_name": "Test Candidate",
    "email": "candidate@example.com",
    "phone": "+91 9000000000",
    "location": "Pune, India",
    "headline": "DevOps Engineer",
    "total_experience_years": 4,
    "github_url": "https://github.com/testcandidate",
    "linkedin_url": "https://linkedin.com/in/testcandidate",
    "target_roles": ["DevOps Engineer", "Platform Engineer"],
    "preferred_locations": ["Pune", "Remote"],
    "excluded_keywords": ["helpdesk"],
    "skills": [
        {"name": "Kubernetes", "years": 3, "evidence": "GKE at Acme", "aliases": ["k8s"]},
        {"name": "Terraform", "years": 3, "evidence": "IaC for all envs"},
        {"name": "Python", "years": 4, "evidence": "Automation tooling"},
        {"name": "GCP", "years": 3, "evidence": "Primary cloud"},
        {"name": "Prometheus", "years": 2, "evidence": "Monitoring at Acme"},
    ],
    "experience": [
        {
            "company": "Acme Corp",
            "title": "DevOps Engineer",
            "start_date": "2022-01",
            "is_current": True,
            "bullets": ["Ran production GKE clusters", "Built Terraform modules"],
            "technologies": ["Kubernetes", "Terraform", "GCP"],
        }
    ],
    "education": [
        {
            "institution": "Pune University",
            "degree": "Bachelor of Engineering",
            "field_of_study": "Computer Science",
            "end_date": "2020-05",
        }
    ],
    "projects": [
        {
            "name": "Cluster Autoscaler",
            "description": "Custom autoscaler",
            "technologies": ["Go", "Kubernetes"],
            "repo_url": "https://github.com/testcandidate/autoscaler",
        }
    ],
}

JD = """
Requirements:
- Must have 3+ years with Kubernetes
- Terraform is required
- Strong Python required

Nice to have:
- Datadog experience is a plus
"""


class StubSource:
    """A source that returns fixed jobs, so no browser is involved."""

    slug = "naukri"
    display_name = "Stub"
    kind = "portal"
    request_delay = 0.0

    def __init__(self, jobs):
        self._jobs = jobs

    async def search_jobs(self, page, query):
        return self._jobs


@asynccontextmanager
async def fake_page(_source):
    yield None


# ====================================================== profile loading

def test_profile_loads_from_json(session):
    profile = load_profile(session, PROFILE_JSON)

    assert profile.full_name == "Test Candidate"
    assert len(profile.skills) == 5
    assert profile.experiences[0].is_current is True
    assert profile.experiences[0].start_date.year == 2022
    assert profile.projects[0].repo_url == "https://github.com/testcandidate/autoscaler"


def test_profile_requires_identity_fields(session):
    """A half-populated profile silently limits what can truthfully be said."""
    with pytest.raises(ProfileFormatError, match="full_name"):
        load_profile(session, {"email": "x@y.com"})


def test_reloading_replaces_rather_than_merges(session):
    """
    A partial merge would leave stale skills behind, and "why does my resume
    still claim X?" is a bad question to have to debug.
    """
    load_profile(session, PROFILE_JSON)
    trimmed = {**PROFILE_JSON, "skills": [{"name": "Kubernetes"}]}
    profile = load_profile(session, trimmed)

    assert len(profile.skills) == 1


# ============================================================ full pass

def test_discovery_through_to_prepared_application(session, settings):
    """
    The section 28 workflow: discover, analyse, match, tailor, prepare —
    stopping exactly where it should, at the safety gates.
    """
    profile = load_profile(session, PROFILE_JSON)
    KnowledgeBase(session, settings=settings).seed_from_profile(profile)

    # --- discover --------------------------------------------------
    raw_jobs = [
        RawJob(
            title="Senior DevOps Engineer", url="https://naukri.com/j/1",
            source_slug="naukri", company="Globex Technologies", location="Pune",
            description=JD, salary_text="18-24 LPA", experience_text="3-5 years",
            easy_apply=True, posted_days_ago=2, skills=["Kubernetes", "Terraform"],
        ),
        RawJob(  # must be filtered out by the excluded keyword
            title="IT Helpdesk Technician", url="https://naukri.com/j/2",
            source_slug="naukri", company="Initech", location="Pune",
            posted_days_ago=1,
        ),
    ]
    discovery = DiscoveryService(session)
    query = SearchQuery(excluded_keywords=profile.excluded_keywords, max_days_old=30)
    outcome = discovery.record_raw_jobs(raw_jobs, query, get_adapter("naukri"))
    session.flush()

    assert outcome.found == 2
    assert outcome.kept == 1  # helpdesk dropped
    assert outcome.new == 1

    # --- analyse ---------------------------------------------------
    counts = AnalysisService(session, profile, settings=settings).analyze_pending()
    session.flush()
    assert counts["analyzed"] == 1
    assert counts["matched"] == 1

    job = session.query(Job).one()
    assert job.status is JobStatus.MATCHED
    assert job.match_score >= settings.min_match_score
    assert "Kubernetes" in job.match_detail["matching_skills"]
    assert "Datadog" in job.match_detail["missing_skills"]

    # --- resume ----------------------------------------------------
    resume_service = ResumeService(session, profile, settings=settings)
    resume_service.generate_master()
    decision = resume_service.decide(job)
    assert isinstance(decision.reason, str) and decision.reason

    # --- prepare ---------------------------------------------------
    engine = ApplicationEngine(session, profile, settings=settings)
    prepared = engine.prepare_application(
        job,
        [
            DetectedQuestion("What is your email address?"),
            DetectedQuestion("What is your expected CTC?"),
            DetectedQuestion("Do you hold a forklift licence?"),
        ],
    )
    session.flush()

    # Seeded from the profile.
    assert prepared.answers["What is your email address?"] == profile.email
    # Sensitive: must be confirmed even though it is derivable.
    assert any(q.text == "What is your expected CTC?" for q in prepared.sensitive)
    # Unknown: must be asked, never guessed.
    assert any(q.text == "Do you hold a forklift licence?" for q in prepared.unanswered)
    assert prepared.ready is False

    # --- nothing was submitted -------------------------------------
    result = asyncio.run(engine.submit_application(prepared, None))
    session.flush()
    assert result.submitted is False
    assert job.status is JobStatus.WAITING_FOR_USER

    # --- user answers, job resumes ---------------------------------
    kb = KnowledgeBase(session, settings=settings)
    for pending in kb.pending_questions(job_id=job.id):
        kb.answer_pending(pending.id, "No" if "forklift" in pending.question_text else "20 LPA")
    session.flush()

    assert engine.resume_after_user_input(job) is True
    assert job.status is JobStatus.READY_TO_APPLY

    # And the answers are now known, so the same questions never stop us again.
    assert kb.resolve("Do you hold a forklift licence?").answer == "No"


def test_duplicate_across_sources_is_one_job(session, settings):
    """Cross-posting must not produce two applications."""
    load_profile(session, PROFILE_JSON)
    discovery = DiscoveryService(session)
    query = SearchQuery()

    discovery.record_raw_jobs(
        [RawJob(title="Senior DevOps Engineer", url="https://naukri.com/a",
                source_slug="naukri", company="Globex Technologies Pvt Ltd",
                location="Pune, Maharashtra")],
        query, get_adapter("naukri"),
    )
    discovery.record_raw_jobs(
        [RawJob(title="DevOps Engineer", url="https://linkedin.com/jobs/view/b",
                source_slug="linkedin", company="Globex Technologies",
                location="Pune")],
        query, get_adapter("linkedin"),
    )
    session.flush()

    assert session.query(Job).count() == 1


def test_a_rejected_job_is_never_applied_to(session, settings):
    profile = load_profile(session, PROFILE_JSON)

    discovery = DiscoveryService(session)
    discovery.record_raw_jobs(
        [RawJob(title="Senior iOS Developer", url="https://n/1", source_slug="naukri",
                company="Initech", location="Chennai",
                description="Requirements:\n- Must have Swift and Kotlin")],
        SearchQuery(), get_adapter("naukri"),
    )
    session.flush()

    AnalysisService(session, profile, settings=settings).analyze_pending()
    session.flush()

    job = session.query(Job).one()
    assert job.status is JobStatus.REJECTED
    # REJECTED is terminal — there is no path from it to an application.
    from app.state import allowed_transitions

    assert allowed_transitions(JobStatus.REJECTED) == frozenset()
