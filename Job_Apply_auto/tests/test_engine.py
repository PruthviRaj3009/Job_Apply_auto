"""
The application engine.

Section 14's defaults (DRY_RUN=true, AUTO_APPLY=false) and section 11's pause
rule are safety properties, not features. Most of these tests try to get an
application submitted when it should not be, and confirm it is not.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.engine import ApplicationEngine, DetectedQuestion, SubmissionResult
from app.models import (
    Application,
    ApplicationStatus,
    EventType,
    JobStatus,
    ResumeKind,
    ResumeVersion,
)
from app.qa import KnowledgeBase

from .conftest import make_job


@pytest.fixture()
def validated_resume(session, profile):
    """A resume that passed validation, so it may be attached."""
    version = ResumeVersion(
        version_id="resume_master_v1",
        kind=ResumeKind.MASTER_RESUME,
        profile_id=profile.id,
        tex_path="/tmp/resume.tex",
        pdf_path="/tmp/resume.pdf",
        compiled=True,
        validated=True,
        page_count=1,
    )
    session.add(version)
    session.flush()
    return version


@pytest.fixture()
def engine(session, profile, settings):
    return ApplicationEngine(session, profile, settings=settings)


@pytest.fixture()
def ready_job(session, validated_resume):
    """A matched job with a validated resume already attached to it."""
    job = make_job(session, fingerprint="ready", title="DevOps Engineer", company="Acme")
    job.status = JobStatus.MATCHED
    validated_resume.job_id = job.id
    session.flush()
    return job


class RecordingDriver:
    """Stands in for a portal driver. Records whether submit was reached."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls: list = []

    async def submit(self, prepared):
        self.calls.append(prepared)
        if self.succeed:
            return SubmissionResult(submitted=True, confirmation="Application sent")
        raise RuntimeError("portal rejected the submission")


def run(coro):
    return asyncio.run(coro)


# ==================================================== preparation

def test_prepare_answers_known_questions(session, engine, ready_job, settings):
    KnowledgeBase(session, settings=settings).store_answer(
        "What is your notice period?", "60 days"
    )
    prepared = engine.prepare_application(
        ready_job, [DetectedQuestion("What is your notice period?")]
    )

    assert prepared.answers["What is your notice period?"] == "60 days"
    assert prepared.unanswered == []


def test_prepare_pauses_on_an_unknown_question(session, engine, ready_job):
    """
    The core of section 11. This is precisely where the old code answered
    "Yes" and submitted.
    """
    prepared = engine.prepare_application(
        ready_job, [DetectedQuestion("Do you hold a commercial pilot licence?")]
    )

    assert prepared.ready is False
    assert len(prepared.unanswered) == 1
    assert prepared.application.status is ApplicationStatus.WAITING_FOR_USER


def test_prepare_pauses_on_a_sensitive_question_even_when_answered(
    session, engine, ready_job, settings
):
    """Section 12: confirmation is required even if a similar answer exists."""
    KnowledgeBase(session, settings=settings).store_answer(
        "Are you willing to relocate?", "Yes"
    )
    prepared = engine.prepare_application(
        ready_job, [DetectedQuestion("Are you willing to relocate?")]
    )

    assert prepared.ready is False
    assert len(prepared.sensitive) == 1
    assert prepared.answers == {}


def test_optional_unknown_question_is_left_blank_not_guessed(session, engine, ready_job):
    """
    Blocking on a question nobody has to answer would stall applications for
    no benefit — but it is still left blank, never filled with a default.
    """
    prepared = engine.prepare_application(
        ready_job,
        [DetectedQuestion("How did you hear about us?", required=False)],
    )

    assert prepared.unanswered == []
    assert "How did you hear about us?" not in prepared.answers


def test_optional_sensitive_question_still_pauses(session, engine, ready_job):
    """Optional does not mean safe — a voluntary disclosure is still a disclosure."""
    prepared = engine.prepare_application(
        ready_job,
        [DetectedQuestion("What is your race/ethnicity?", required=False)],
    )
    assert len(prepared.sensitive) == 1


def test_unanswered_questions_reach_the_pending_queue(session, engine, ready_job, settings):
    engine.prepare_application(
        ready_job, [DetectedQuestion("Do you hold a forklift licence?")]
    )
    pending = KnowledgeBase(session, settings=settings).pending_questions(job_id=ready_job.id)

    assert len(pending) == 1
    assert pending[0].question_text == "Do you hold a forklift licence?"


def test_preparation_without_a_validated_resume_is_blocked(session, engine, settings):
    """Section 9: a resume that failed validation is never attached."""
    job = make_job(session, fingerprint="noresume", title="DevOps Engineer")
    job.status = JobStatus.MATCHED
    session.flush()

    prepared = engine.prepare_application(job, [])
    assert prepared.ready is False
    assert any("resume" in b.lower() for b in prepared.blockers)


def test_events_are_logged_for_each_question(session, engine, ready_job, settings):
    from app.models import ApplicationEvent

    KnowledgeBase(session, settings=settings).store_answer("Known question?", "Yes")
    engine.prepare_application(
        ready_job,
        [DetectedQuestion("Known question?"), DetectedQuestion("Unknown question?")],
    )
    session.flush()

    types = {
        e.event_type
        for e in session.query(ApplicationEvent).filter_by(job_id=ready_job.id).all()
    }
    assert EventType.QUESTION_FOUND in types
    assert EventType.ANSWER_RETRIEVED in types
    assert EventType.USER_INPUT_REQUIRED in types


# ===================================================== submission gates

def test_nothing_is_submitted_by_default(session, engine, ready_job):
    """
    DRY_RUN=true and AUTO_APPLY=false are the shipped defaults (section 14).
    """
    prepared = engine.prepare_application(ready_job, [])
    driver = RecordingDriver()
    result = run(engine.submit_application(prepared, driver))

    assert result.submitted is False
    assert result.dry_run is True
    assert driver.calls == []  # the driver was never even reached


def test_dry_run_alone_still_blocks(session, profile, settings, ready_job):
    settings.auto_apply = True
    settings.dry_run = True
    engine = ApplicationEngine(session, profile, settings=settings)

    prepared = engine.prepare_application(ready_job, [])
    driver = RecordingDriver()
    result = run(engine.submit_application(prepared, driver))

    assert result.submitted is False
    assert driver.calls == []


def test_auto_apply_off_alone_still_blocks(session, profile, settings, ready_job):
    settings.auto_apply = False
    settings.dry_run = False
    engine = ApplicationEngine(session, profile, settings=settings)

    prepared = engine.prepare_application(ready_job, [])
    driver = RecordingDriver()
    result = run(engine.submit_application(prepared, driver))

    assert result.submitted is False
    assert driver.calls == []


def test_submission_happens_when_both_gates_are_open(session, profile, settings, ready_job):
    settings.auto_apply = True
    settings.dry_run = False
    engine = ApplicationEngine(session, profile, settings=settings)

    prepared = engine.prepare_application(ready_job, [])
    driver = RecordingDriver()
    result = run(engine.submit_application(prepared, driver))

    assert result.submitted is True
    assert len(driver.calls) == 1
    assert ready_job.status is JobStatus.SUBMITTED
    assert prepared.application.status is ApplicationStatus.SUBMITTED
    assert prepared.application.submitted_at is not None


def test_an_unanswered_question_blocks_submission_even_with_gates_open(
    session, profile, settings, ready_job
):
    """
    The decisive test. Both safety switches are off, and the application still
    must not go out with a question nobody answered.
    """
    settings.auto_apply = True
    settings.dry_run = False
    engine = ApplicationEngine(session, profile, settings=settings)

    prepared = engine.prepare_application(
        ready_job, [DetectedQuestion("Do you hold a forklift licence?")]
    )
    driver = RecordingDriver()
    result = run(engine.submit_application(prepared, driver))

    assert result.submitted is False
    assert result.needs_user_input is True
    assert driver.calls == []
    assert ready_job.status is JobStatus.WAITING_FOR_USER


def test_a_sensitive_question_blocks_submission_even_with_gates_open(
    session, profile, settings, ready_job
):
    settings.auto_apply = True
    settings.dry_run = False
    engine = ApplicationEngine(session, profile, settings=settings)

    prepared = engine.prepare_application(
        ready_job, [DetectedQuestion("Are you legally authorized to work here?")]
    )
    result = run(engine.submit_application(prepared, RecordingDriver()))

    assert result.submitted is False
    assert result.needs_user_input is True


def test_duplicate_application_is_prevented(session, profile, settings, ready_job):
    settings.auto_apply = True
    settings.dry_run = False
    engine = ApplicationEngine(session, profile, settings=settings)

    prepared = engine.prepare_application(ready_job, [])
    run(engine.submit_application(prepared, RecordingDriver()))
    session.flush()

    assert engine._already_applied(ready_job) is True


def test_company_cap_is_enforced_across_runs(session, profile, settings, validated_resume):
    """
    Counted from the database rather than per-batch state, so the cap holds
    across consecutive runs instead of resetting each time.
    """
    settings.max_applications_per_company = 1
    settings.company_window_days = 1
    engine = ApplicationEngine(session, profile, settings=settings)

    session.add(
        Application(
            job_id=make_job(session, fingerprint="prev", company="Acme Technologies Pvt Ltd").id,
            company="Acme Technologies Pvt Ltd",
            job_title="Other Role",
            status=ApplicationStatus.SUBMITTED,
            submitted_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
    )
    session.flush()

    job = make_job(session, fingerprint="capped", company="Acme", title="DevOps Engineer")
    validated_resume.job_id = job.id
    session.flush()

    # Normalized company matching: "Acme" and "Acme Technologies Pvt Ltd" are
    # the same employer.
    assert engine._company_cap_reached(job) is True


def test_old_applications_fall_outside_the_company_window(
    session, profile, settings, validated_resume
):
    settings.max_applications_per_company = 1
    settings.company_window_days = 1
    engine = ApplicationEngine(session, profile, settings=settings)

    session.add(
        Application(
            job_id=make_job(session, fingerprint="old", company="Acme").id,
            company="Acme", job_title="Old Role",
            status=ApplicationStatus.SUBMITTED,
            submitted_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
    )
    session.flush()

    job = make_job(session, fingerprint="fresh", company="Acme")
    assert engine._company_cap_reached(job) is False


# ======================================================== failures

def test_driver_failure_is_recorded_not_raised(session, profile, settings, ready_job):
    """One failed application must never crash a batch (section 30)."""
    from app.models import ApplicationError

    settings.auto_apply = True
    settings.dry_run = False
    engine = ApplicationEngine(session, profile, settings=settings)

    prepared = engine.prepare_application(ready_job, [])
    result = run(engine.submit_application(prepared, RecordingDriver(succeed=False)))

    assert result.submitted is False
    assert "portal rejected" in result.error
    assert ready_job.status is JobStatus.FAILED

    error = session.query(ApplicationError).filter_by(job_id=ready_job.id).one()
    assert error.stage == "submit"
    assert error.traceback


def test_submission_without_a_driver_does_not_claim_success(
    session, profile, settings, ready_job
):
    settings.auto_apply = True
    settings.dry_run = False
    engine = ApplicationEngine(session, profile, settings=settings)

    prepared = engine.prepare_application(ready_job, [])
    result = run(engine.submit_application(prepared, None))

    assert result.submitted is False
    assert "driver" in result.error.lower()


# ========================================================= resuming

def test_job_resumes_once_every_question_is_answered(
    session, profile, settings, ready_job
):
    """The "STORE Q&A -> CONTINUE" half of the section 11 loop."""
    engine = ApplicationEngine(session, profile, settings=settings)
    kb = KnowledgeBase(session, settings=settings)

    prepared = engine.prepare_application(
        ready_job, [DetectedQuestion("Do you hold a forklift licence?")]
    )
    run(engine.submit_application(prepared, RecordingDriver()))
    session.flush()
    assert ready_job.status is JobStatus.WAITING_FOR_USER

    # Still waiting while the question is open.
    assert engine.resume_after_user_input(ready_job) is False

    pending = kb.pending_questions(job_id=ready_job.id)[0]
    kb.answer_pending(pending.id, "No")
    session.flush()

    assert engine.resume_after_user_input(ready_job) is True
    assert ready_job.status is JobStatus.READY_TO_APPLY
    # And the answer is now known for next time.
    assert kb.resolve("Do you hold a forklift licence?").answer == "No"


def test_preparing_twice_reuses_the_open_attempt(session, engine, ready_job):
    """Re-running a paused application must not open a second attempt."""
    first = engine.prepare_application(ready_job, [])
    second = engine.prepare_application(ready_job, [])

    assert first.application.id == second.application.id
    assert session.query(Application).filter_by(job_id=ready_job.id).count() == 1
