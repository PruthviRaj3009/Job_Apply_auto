"""Database layer: constraints, cascades, event log and guarded transitions."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import last_event, log_event, move_job, session_scope
from app.models import (
    Answer,
    Application,
    ApplicationEvent,
    EventType,
    Job,
    JobStatus,
    PendingQuestion,
    Question,
    Skill,
)
from app.state import IllegalTransition

from .conftest import make_job


def test_fingerprint_is_unique(session):
    """
    Duplicate prevention rests on this constraint (section 5). Without it the
    same role cross-posted to three portals becomes three applications.
    """
    make_job(session, fingerprint="dup", url="https://a.example/1")
    session.commit()

    session.add(
        Job(fingerprint="dup", url="https://b.example/2", title="DevOps", company="Acme")
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_foreign_keys_are_enforced(session):
    """
    SQLite ignores foreign keys unless the pragma is set; the session layer
    sets it. An unenforced FK would let applications outlive their job.
    """
    session.add(Application(job_id=999_999, company="Ghost", job_title="X"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_deleting_a_job_cascades_to_its_applications(session):
    job = make_job(session, fingerprint="cascade")
    session.add(Application(job_id=job.id, company="Acme", job_title="DevOps"))
    session.flush()
    assert session.query(Application).count() == 1

    session.delete(job)
    session.flush()
    assert session.query(Application).count() == 0


def test_move_job_logs_an_event(session):
    job = make_job(session, fingerprint="ev")
    move_job(session, job, JobStatus.ANALYZING)
    move_job(session, job, JobStatus.MATCHED, note="score 0.81")
    session.flush()

    events = session.query(ApplicationEvent).filter_by(job_id=job.id).all()
    types = {e.event_type for e in events}
    assert EventType.JOB_MATCHED in types
    assert job.status == JobStatus.MATCHED
    assert job.status_note == "score 0.81"


def test_move_job_rejects_an_illegal_transition(session):
    job = make_job(session, fingerprint="illegal")
    with pytest.raises(IllegalTransition):
        move_job(session, job, JobStatus.SUBMITTED)
    assert job.status == JobStatus.NEW


def test_move_job_to_same_status_logs_nothing(session):
    """Re-running a step must not fill the log with no-op entries."""
    job = make_job(session, fingerprint="noop")
    move_job(session, job, JobStatus.ANALYZING)
    session.flush()
    before = session.query(ApplicationEvent).filter_by(job_id=job.id).count()

    move_job(session, job, JobStatus.ANALYZING)
    session.flush()
    assert session.query(ApplicationEvent).filter_by(job_id=job.id).count() == before


def test_last_event_identifies_the_resume_point(session):
    """After a crash, the newest event says which step to pick back up."""
    job = make_job(session, fingerprint="restart")
    move_job(session, job, JobStatus.ANALYZING)
    move_job(session, job, JobStatus.MATCHED)
    move_job(session, job, JobStatus.READY_TO_APPLY)
    move_job(session, job, JobStatus.APPLYING)
    session.flush()

    assert last_event(session, job.id).event_type == EventType.APPLICATION_STARTED


def test_normalized_question_is_unique(session):
    """The dedupe key for the Q&A knowledge base (section 10)."""
    session.add(Question(question="Years of Python?", normalized_question="years of python"))
    session.commit()

    session.add(Question(question="How many years Python?", normalized_question="years of python"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_answers_cascade_from_their_question(session):
    q = Question(question="Notice period?", normalized_question="notice period")
    session.add(q)
    session.flush()
    session.add(Answer(question_id=q.id, answer="60 days"))
    session.flush()

    session.delete(q)
    session.flush()
    assert session.query(Answer).count() == 0


def test_event_log_survives_across_sessions(settings):
    """
    The log has to be durable, not per-session state — a restarted process
    reads it to decide what to do next.
    """
    with session_scope(settings) as s:
        job = make_job(s, fingerprint="durable")
        job_id = job.id
        log_event(s, EventType.JOB_DISCOVERED, job_id=job_id, message="found")

    with session_scope(settings) as s:
        assert last_event(s, job_id).message == "found"


def test_pending_question_defaults_to_unresolved(session):
    job = make_job(session, fingerprint="pending")
    pq = PendingQuestion(job_id=job.id, question_text="Are you authorized to work in the US?")
    session.add(pq)
    session.flush()
    assert pq.resolved is False
    assert pq.resolved_at is None


def test_profile_skills_cascade(session, profile):
    assert session.query(Skill).count() == 4
    session.delete(profile)
    session.flush()
    assert session.query(Skill).count() == 0


def test_job_defaults(session):
    job = make_job(session, fingerprint="defaults")
    assert job.status == JobStatus.NEW
    assert job.skills == []
    assert job.easy_apply is False
    assert job.match_score is None
