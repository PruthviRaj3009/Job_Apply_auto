"""Analysis service: pipeline transitions, thresholds and failure isolation."""

from __future__ import annotations

from app.models import ApplicationError, ApplicationEvent, EventType, JobStatus
from app.services.analysis import AnalysisService

from .conftest import make_job
from .test_matching import FULL_JD


def test_matched_job_moves_to_matched(session, profile, settings):
    svc = AnalysisService(session, profile, settings=settings)
    job = make_job(
        session, fingerprint="a1", title="DevOps Engineer",
        location="Pune", description=FULL_JD, experience_min_years=3,
    )
    svc.analyze_job(job)
    session.flush()

    assert job.status == JobStatus.MATCHED
    assert job.match_score >= settings.min_match_score
    assert job.match_detail["match_explanation"]


def test_low_scoring_job_is_rejected_with_a_reason(session, profile, settings):
    svc = AnalysisService(session, profile, settings=settings)
    job = make_job(
        session, fingerprint="a2", title="Senior iOS Developer",
        location="Chennai", description="Requirements:\n- Must have Swift and Kotlin",
    )
    svc.analyze_job(job)
    session.flush()

    assert job.status == JobStatus.REJECTED
    assert "below threshold" in job.status_note


def test_analysis_writes_structure_back_onto_the_job(session, profile, settings):
    svc = AnalysisService(session, profile, settings=settings)
    job = make_job(session, fingerprint="a3", title="DevOps Engineer", description=FULL_JD)
    svc.analyze_job(job)
    session.flush()

    assert "Kubernetes" in job.skills
    assert job.mandatory_requirements
    assert job.responsibilities


def test_source_supplied_skills_are_preserved(session, profile, settings):
    """
    A portal's own skill tags are often better than the prose. Analysis adds
    to them rather than replacing them.
    """
    svc = AnalysisService(session, profile, settings=settings)
    job = make_job(
        session, fingerprint="a4", title="DevOps Engineer",
        description=FULL_JD, skills=["Kubernetes", "SomeNicheTool"],
    )
    svc.analyze_job(job)
    session.flush()

    assert "SomeNicheTool" in job.skills
    assert "Terraform" in job.skills  # added from the JD


def test_analysis_logs_an_event(session, profile, settings):
    svc = AnalysisService(session, profile, settings=settings)
    job = make_job(session, fingerprint="a5", title="DevOps Engineer", description=FULL_JD)
    svc.analyze_job(job)
    session.flush()

    types = {
        e.event_type
        for e in session.query(ApplicationEvent).filter_by(job_id=job.id).all()
    }
    assert EventType.JOB_ANALYZED in types


def test_batch_continues_past_a_failing_job(session, profile, settings):
    """
    Section 30: one failed job must never crash the whole process.
    """
    svc = AnalysisService(session, profile, settings=settings)
    good = make_job(session, fingerprint="b1", title="DevOps Engineer", description=FULL_JD)
    bad = make_job(session, fingerprint="b2", title="DevOps Engineer", description=FULL_JD)
    session.flush()

    original = svc.engine.match

    def explode(job, parsed=None):
        if job.id == bad.id:
            raise RuntimeError("synthetic matching failure")
        return original(job, parsed)

    svc.engine.match = explode
    counts = svc.analyze_pending()
    session.flush()

    assert counts["failed"] == 1
    assert counts["analyzed"] == 1
    assert good.status in (JobStatus.MATCHED, JobStatus.REJECTED)


def test_a_failed_job_is_parked_not_stranded(session, profile, settings):
    """
    Leaving it in ANALYZING would strand it forever — the batch query only
    selects NEW and SEEN.
    """
    svc = AnalysisService(session, profile, settings=settings)
    job = make_job(session, fingerprint="b3", title="DevOps Engineer", description=FULL_JD)
    session.flush()

    svc.engine.match = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    svc.analyze_pending()
    session.flush()

    assert job.status == JobStatus.FAILED
    error = session.query(ApplicationError).filter_by(job_id=job.id).one()
    assert error.stage == "analysis"
    assert "boom" in error.message


def test_threshold_is_configurable(session, profile, settings, monkeypatch):
    """A stricter threshold rejects a job a looser one accepts."""
    job = make_job(
        session, fingerprint="thr", title="DevOps Engineer",
        location="Pune", description=FULL_JD, experience_min_years=3,
    )
    settings.min_match_score = 0.99
    AnalysisService(session, profile, settings=settings).analyze_job(job)
    session.flush()

    assert job.status == JobStatus.REJECTED


def test_matched_jobs_are_returned_best_first(session, profile, settings):
    svc = AnalysisService(session, profile, settings=settings)
    make_job(session, fingerprint="q1", title="DevOps Engineer",
             location="Pune", description=FULL_JD, experience_min_years=3)
    make_job(session, fingerprint="q2", title="Platform Engineer",
             location="Remote", description=FULL_JD, experience_min_years=3)
    session.flush()

    svc.analyze_pending()
    session.flush()

    scores = [j.match_score for j in svc.matched_jobs()]
    assert scores == sorted(scores, reverse=True)
