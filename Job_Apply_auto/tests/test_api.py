"""API surface: routing, auth, and that it cannot bypass the engine's rules."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.db import get_db
from app.models import Application, ApplicationStatus, JobStatus, PendingQuestion
from app.qa import KnowledgeBase

from .conftest import make_job
from .test_matching import FULL_JD


@pytest.fixture()
def client(session, settings, profile):
    """
    A client bound to the test session.

    The DB dependency is overridden rather than letting the app open its own
    session, so assertions can be made against the same transaction.
    """
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as c:
        yield c


# ================================================================ auth

def test_no_key_required_on_loopback_by_default(client):
    assert client.get("/api/health").status_code == 200


def test_key_is_enforced_when_configured(session, settings, profile):
    settings.api_key = "s3cret"
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/health", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/api/health", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_public_bind_without_a_key_is_refused_at_startup(settings, monkeypatch):
    """
    Serving an unauthenticated API that can spend money and act as the user
    is not something to allow by accident.
    """
    settings.api_host = "0.0.0.0"
    settings.api_key = ""
    with pytest.raises(RuntimeError, match="API_KEY"):
        _ = settings.require_api_key


# ================================================================ jobs

def test_list_and_filter_jobs(client, session):
    make_job(session, fingerprint="j1", title="DevOps Engineer", company="Acme", match_score=0.9)
    j2 = make_job(session, fingerprint="j2", title="SRE", company="Globex", match_score=0.4)
    j2.status = JobStatus.REJECTED
    session.flush()

    assert len(client.get("/api/jobs").json()) == 2
    assert len(client.get("/api/jobs", params={"status": "REJECTED"}).json()) == 1
    assert len(client.get("/api/jobs", params={"company": "acme"}).json()) == 1
    assert len(client.get("/api/jobs", params={"min_score": 0.8}).json()) == 1


def test_jobs_are_returned_best_first(client, session):
    make_job(session, fingerprint="lo", title="A", match_score=0.3)
    make_job(session, fingerprint="hi", title="B", match_score=0.9)
    session.flush()

    scores = [j["match_score"] for j in client.get("/api/jobs").json()]
    assert scores == sorted(scores, reverse=True)


def test_job_detail_and_404(client, session):
    job = make_job(session, fingerprint="d1", title="DevOps Engineer", description=FULL_JD)
    session.flush()

    assert client.get(f"/api/jobs/{job.id}").json()["title"] == "DevOps Engineer"
    assert client.get("/api/jobs/99999").status_code == 404


def test_analyze_endpoint_scores_a_job(client, session):
    job = make_job(
        session, fingerprint="an", title="DevOps Engineer",
        location="Pune", description=FULL_JD, experience_min_years=3,
    )
    session.flush()

    body = client.post(f"/api/jobs/{job.id}/analyze").json()
    assert body["match_score"] is not None
    assert body["status"] in ("MATCHED", "REJECTED")


def test_match_endpoint_refuses_before_analysis(client, session):
    """A 409 says "not yet", which is actionable; an empty 200 does not."""
    job = make_job(session, fingerprint="nm", title="DevOps Engineer")
    session.flush()
    assert client.get(f"/api/jobs/{job.id}/match").status_code == 409


def test_search_rejects_an_unknown_source(client):
    response = client.post("/api/jobs/search", json={"keywords": ["devops"], "sources": ["monster"]})
    assert response.status_code == 400
    assert "naukri" in response.json()["detail"]


# ========================================================= applications

def test_apply_reports_blockers_rather_than_submitting(client, session, settings):
    """
    The API prepares; it never submits. Routing submission through HTTP would
    invite a client to drive it past the safety gates.
    """
    job = make_job(session, fingerprint="ap", title="DevOps Engineer")
    job.status = JobStatus.MATCHED
    application = Application(job_id=job.id, company="Acme", job_title="DevOps Engineer")
    session.add(application)
    session.flush()

    body = client.post(f"/api/applications/{application.id}/apply").json()
    assert body["detail"]["ready"] is False
    assert any("AUTO_APPLY" in b for b in body["detail"]["blockers"])


def test_retry_refuses_an_already_submitted_job(client, session):
    job = make_job(session, fingerprint="rt", title="DevOps Engineer")
    job.status = JobStatus.SUBMITTED
    application = Application(
        job_id=job.id, company="Acme", job_title="DevOps Engineer",
        status=ApplicationStatus.SUBMITTED,
    )
    session.add(application)
    session.flush()

    response = client.post(f"/api/applications/{application.id}/retry")
    assert response.status_code == 409
    assert "already submitted" in response.json()["detail"]


# ============================================================ questions

def test_pending_questions_are_listed(client, session, settings):
    kb = KnowledgeBase(session, settings=settings)
    resolution = kb.resolve("Do you hold a forklift licence?")
    kb.raise_pending("Do you hold a forklift licence?", resolution)
    session.flush()

    body = client.get("/api/questions/pending").json()
    assert len(body) == 1
    assert body[0]["question_text"] == "Do you hold a forklift licence?"


def test_answering_stores_and_releases(client, session, settings):
    """The "STORE Q&A -> CONTINUE" half of section 11's loop, over HTTP."""
    job = make_job(session, fingerprint="q1", title="DevOps Engineer")
    job.status = JobStatus.WAITING_FOR_USER
    session.flush()

    kb = KnowledgeBase(session, settings=settings)
    resolution = kb.resolve("Do you hold a forklift licence?")
    pending = kb.raise_pending("Do you hold a forklift licence?", resolution, job_id=job.id)
    session.flush()

    body = client.post(f"/api/questions/{pending.id}/answer", json={"answer": "No"}).json()
    assert body["detail"]["job_released"] is True
    assert job.status is JobStatus.READY_TO_APPLY
    # And it is never asked again.
    assert kb.resolve("Do you hold a forklift licence?").answer == "No"


def test_answer_requires_content(client, session, settings):
    kb = KnowledgeBase(session, settings=settings)
    pending = kb.raise_pending("Anything?", kb.resolve("Anything?"))
    session.flush()

    assert client.post(f"/api/questions/{pending.id}/answer", json={"answer": ""}).status_code == 422


def test_answering_a_missing_question_404s(client):
    assert client.post("/api/questions/99999/answer", json={"answer": "x"}).status_code == 404


def test_sensitive_questions_are_marked_for_the_ui(client, session, settings):
    """The dashboard needs to show why it is asking."""
    kb = KnowledgeBase(session, settings=settings)
    question = "Are you legally authorized to work in the US?"
    kb.raise_pending(question, kb.resolve(question))
    session.flush()

    assert client.get("/api/questions/pending").json()[0]["is_sensitive"] is True


# ====================================================== dashboard etc.

def test_dashboard_reports_the_section_19_counts(client, session):
    make_job(session, fingerprint="d", title="DevOps Engineer")
    session.flush()

    counts = client.get("/api/dashboard").json()["counts"]
    for key in (
        "total_jobs", "new_jobs", "matched_jobs", "rejected_jobs",
        "applications_prepared", "submitted", "failed", "waiting_for_user",
        "interview_invitations", "rejections",
    ):
        assert key in counts
    assert counts["total_jobs"] == 1


def test_analytics_includes_the_actionable_numbers(client, session):
    make_job(
        session, fingerprint="a", title="DevOps Engineer", match_score=0.75,
        match_detail={"missing_skills": ["Kafka", "Snowflake"]},
    )
    session.flush()

    body = client.get("/api/analytics").json()
    assert body["match_score_distribution"]["0.6-0.8"] == 1
    assert ["Kafka", 1] in body["top_missing_skills"]


def test_settings_endpoint_hides_secrets(client):
    """
    This exists so the dashboard can show whether auto-apply is on, not so a
    client can enumerate the deployment.
    """
    body = client.get("/api/settings").json()
    assert body["dry_run"] is True
    assert body["auto_apply"] is False
    for secret in ("api_key", "database_url", "gmail_credentials_file"):
        assert secret not in body


def test_sources_endpoint_lists_every_adapter(client):
    sources = client.get("/api/sources").json()
    assert {"linkedin", "naukri", "careerpage"}.issubset(set(sources))


def test_profile_endpoint(client, profile):
    assert client.get("/api/profile").json()["full_name"] == profile.full_name


def test_missing_profile_gives_an_actionable_error(session, settings):
    """A 412 naming the problem beats a 500 from a None dereference."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as c:
        response = c.get("/api/profile")
        assert response.status_code == 412
        assert "profile" in response.json()["detail"].lower()
