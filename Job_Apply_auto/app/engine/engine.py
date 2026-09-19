"""
The unified application engine (section 14).

One engine, one set of rules, whatever portal is underneath. The portal
differences live in `app.sources` adapters and the browser driver; what
happens *to an application* is decided here.

Three invariants, in order of importance:

1. **Nothing is submitted while a question is unanswered.** Not a default, not
   a best guess, not "Yes". `prepare_application` collects every question it
   cannot answer from the user's own data and returns WAITING_FOR_USER.
2. **Nothing is submitted unless AUTO_APPLY is on and DRY_RUN is off.** Both
   default to the safe setting, and the check happens at the moment of
   submission, not at the start of the run.
3. **Nothing is submitted with an unvalidated resume.** A resume that failed
   validation is never attached (section 9).
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.events import log_event, move_job
from ..models import (
    Application,
    ApplicationError,
    ApplicationStatus,
    EventType,
    Job,
    JobStatus,
    Profile,
    ResumeVersion,
)
from ..qa import AnswerResolution, KnowledgeBase
from ..sources.normalize import normalize_company

logger = logging.getLogger(__name__)


class FormQuestion(Protocol):
    """A question the browser layer found on a form."""

    text: str
    field_type: str
    options: list[str]
    required: bool


@dataclass(slots=True)
class DetectedQuestion:
    """Concrete `FormQuestion`, produced by the portal driver."""

    text: str
    field_type: str = "text"
    options: list[str] = field(default_factory=list)
    required: bool = True
    #: Selector or handle the driver uses to fill it.
    locator: str = ""


@dataclass(slots=True)
class PreparedApplication:
    """
    Everything needed to submit — or the reasons we cannot.

    `ready` is false whenever anything is outstanding. Callers check that one
    flag rather than re-deriving readiness, so there is one definition of
    "safe to submit" in the system.
    """

    job: Job
    application: Application
    answers: dict[str, str] = field(default_factory=dict)
    unanswered: list[DetectedQuestion] = field(default_factory=list)
    sensitive: list[DetectedQuestion] = field(default_factory=list)
    resume: ResumeVersion | None = None
    cover_letter_version: str | None = None
    blockers: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.unanswered and not self.sensitive and not self.blockers

    @property
    def outstanding_questions(self) -> list[DetectedQuestion]:
        return [*self.unanswered, *self.sensitive]


@dataclass(slots=True)
class SubmissionResult:
    submitted: bool
    dry_run: bool = False
    confirmation: str = ""
    error: str = ""
    needs_user_input: bool = False
    questions: list[str] = field(default_factory=list)


class ApplicationEngine:
    """
    Drives one application from prepared to submitted.

    The portal driver is injected. It is responsible only for mechanics —
    find the fields, fill them, click submit — and never for deciding *what*
    to answer. That separation is why a new portal cannot reintroduce a
    guessing fallback.
    """

    def __init__(
        self,
        session: Session,
        profile: Profile,
        *,
        settings: Settings | None = None,
        knowledge_base: KnowledgeBase | None = None,
        resume_service: Any | None = None,
    ) -> None:
        self.session = session
        self.profile = profile
        self.settings = settings or get_settings()
        self.kb = knowledge_base or KnowledgeBase(session, settings=self.settings)
        self.resume_service = resume_service

    # ----------------------------------------------------------- prepare

    def prepare_application(
        self,
        job: Job,
        questions: list[DetectedQuestion] | None = None,
        *,
        platform: str | None = None,
    ) -> PreparedApplication:
        """
        Work out what this application needs and whether we can supply it.

        Answers come only from the knowledge base, which answers only from the
        user's own data. A question it cannot answer becomes an outstanding
        question, never a default.
        """
        self._begin_attempt(job)
        application = self._open_application(job)
        prepared = PreparedApplication(job=job, application=application)
        platform = platform or (job.source.slug if job.source else "")

        # --- resume ---------------------------------------------------
        prepared.resume = self._select_resume(job)
        if prepared.resume is None:
            prepared.blockers.append(
                "No validated resume is available for this job. A resume that "
                "failed validation is never attached."
            )
        else:
            application.resume_version = prepared.resume.version_id

        # --- questions -------------------------------------------------
        for question in questions or []:
            log_event(
                self.session,
                EventType.QUESTION_FOUND,
                job_id=job.id,
                application_id=application.id,
                message=question.text[:200],
            )
            resolution = self.answer_question(question, platform=platform)

            if resolution.resolved:
                prepared.answers[question.text] = resolution.answer or ""
                log_event(
                    self.session,
                    EventType.ANSWER_RETRIEVED,
                    job_id=job.id,
                    application_id=application.id,
                    message=f"{question.text[:80]} -> {resolution.answer}",
                    payload={"confidence": resolution.confidence},
                )
                continue

            # Optional questions we cannot answer are simply left blank —
            # that is honest, and blocking on them would stall applications
            # over questions nobody has to answer.
            if not question.required and not resolution.is_sensitive:
                continue

            self.request_user_input(job, application, question, resolution)
            if resolution.is_sensitive:
                prepared.sensitive.append(question)
            else:
                prepared.unanswered.append(question)

        # --- record ----------------------------------------------------
        application.answered_questions = [
            {"question": q, "answer": a} for q, a in prepared.answers.items()
        ]
        application.pending_questions = [q.text for q in prepared.outstanding_questions]
        application.status = (
            ApplicationStatus.PREPARED if prepared.ready else ApplicationStatus.WAITING_FOR_USER
        )
        self.session.flush()
        return prepared

    def answer_question(
        self, question: DetectedQuestion, *, platform: str | None = None
    ) -> AnswerResolution:
        """
        Resolve one question. Delegates entirely to the knowledge base.

        There is no fallback path here by design: the engine has no way to
        answer a question the knowledge base could not, so it cannot invent
        one (section 11).
        """
        return self.kb.resolve(
            question.text,
            field_type=question.field_type,
            options=question.options,
            platform=platform,
        )

    def request_user_input(
        self,
        job: Job,
        application: Application,
        question: DetectedQuestion,
        resolution: AnswerResolution,
    ) -> None:
        """Queue a question for the user and log why we stopped."""
        self.kb.raise_pending(
            question.text,
            resolution,
            job_id=job.id,
            application_id=application.id,
            field_type=question.field_type,
            options=question.options,
        )
        log_event(
            self.session,
            EventType.USER_INPUT_REQUIRED,
            job_id=job.id,
            application_id=application.id,
            message=question.text[:200],
            payload={"reason": resolution.reason, "sensitive": resolution.is_sensitive},
        )

    # ---------------------------------------------------------- validate

    def validate_application(self, prepared: PreparedApplication) -> list[str]:
        """
        Everything standing between this application and submission.

        Returns reasons rather than a boolean: an empty list means go, and a
        non-empty one is exactly what to tell the user.
        """
        reasons = list(prepared.blockers)

        if prepared.sensitive:
            reasons.append(
                f"{len(prepared.sensitive)} sensitive question(s) need your explicit "
                "confirmation before this can be submitted."
            )
        if prepared.unanswered:
            reasons.append(
                f"{len(prepared.unanswered)} question(s) could not be answered "
                "from your profile."
            )
        if prepared.resume is None or not prepared.resume.validated:
            reasons.append("No validated resume is attached.")

        if not self.settings.auto_apply:
            reasons.append("AUTO_APPLY is disabled, so nothing will be submitted.")
        if self.settings.dry_run:
            reasons.append("DRY_RUN is enabled, so nothing will be submitted.")

        if self._company_cap_reached(prepared.job):
            reasons.append(
                f"Already applied to {prepared.job.company} "
                f"{self.settings.max_applications_per_company} time(s) in the last "
                f"{self.settings.company_window_days} day(s)."
            )
        if self._already_applied(prepared.job):
            reasons.append("An application to this job has already been submitted.")

        return reasons

    # ------------------------------------------------------------ submit

    async def submit_application(
        self,
        prepared: PreparedApplication,
        driver: Any | None = None,
    ) -> SubmissionResult:
        """
        Submit, but only when every gate is open.

        The gates are re-checked here rather than trusted from `prepare`:
        preparation and submission can be minutes apart, and the user may
        have changed a setting or another run may have applied in between.
        """
        job = prepared.job
        application = prepared.application

        if prepared.outstanding_questions:
            move_job(self.session, job, JobStatus.WAITING_FOR_USER,
                     note=f"{len(prepared.outstanding_questions)} question(s) awaiting your answer")
            return SubmissionResult(
                submitted=False,
                needs_user_input=True,
                error="Paused: questions need your answer before this can be submitted.",
                questions=[q.text for q in prepared.outstanding_questions],
            )

        blockers = self.validate_application(prepared)
        gating = [b for b in blockers if "AUTO_APPLY" in b or "DRY_RUN" in b]

        if gating:
            # Not a failure — the system is doing exactly what it was told.
            application.status = ApplicationStatus.PREPARED
            application.dry_run = True
            self.session.flush()
            return SubmissionResult(
                submitted=False,
                dry_run=True,
                confirmation=(
                    "Dry run: application fully prepared and not submitted. "
                    + " ".join(gating)
                ),
            )

        if blockers:
            application.status = ApplicationStatus.FAILED
            application.error_message = "; ".join(blockers)
            move_job(self.session, job, JobStatus.FAILED, note=application.error_message)
            self.session.flush()
            return SubmissionResult(submitted=False, error="; ".join(blockers))

        if driver is None:
            return SubmissionResult(
                submitted=False,
                error="No portal driver supplied; cannot submit.",
            )

        # --- actually submit -------------------------------------------
        try:
            outcome = await driver.submit(prepared)
        except Exception as exc:  # noqa: BLE001 - one failure must not kill a batch
            logger.exception("Submission failed for job %s", job.id)
            self.record_result(prepared, SubmissionResult(submitted=False, error=str(exc)), exc=exc)
            return SubmissionResult(submitted=False, error=str(exc))

        self.record_result(prepared, outcome)
        return outcome

    def record_result(
        self,
        prepared: PreparedApplication,
        outcome: SubmissionResult,
        *,
        exc: Exception | None = None,
    ) -> None:
        """Persist the outcome and move the job to its resting state."""
        job = prepared.job
        application = prepared.application

        if outcome.submitted:
            application.status = ApplicationStatus.SUBMITTED
            application.submitted_at = datetime.now(timezone.utc)
            application.confirmation = outcome.confirmation
            application.dry_run = False
            move_job(self.session, job, JobStatus.SUBMITTED, note=outcome.confirmation)
        elif outcome.needs_user_input:
            application.status = ApplicationStatus.WAITING_FOR_USER
            application.pending_questions = outcome.questions
            move_job(self.session, job, JobStatus.WAITING_FOR_USER, note=outcome.error)
        else:
            application.status = ApplicationStatus.FAILED
            application.error_message = outcome.error
            move_job(self.session, job, JobStatus.FAILED, note=outcome.error)
            log_event(
                self.session,
                EventType.APPLICATION_FAILED,
                job_id=job.id,
                application_id=application.id,
                message=outcome.error[:500],
            )
            if exc is not None:
                self.session.add(
                    ApplicationError(
                        job_id=job.id,
                        application_id=application.id,
                        stage="submit",
                        error_type=type(exc).__name__,
                        message=str(exc),
                        traceback=traceback.format_exc(),
                        page_url=job.application_url or job.url,
                    )
                )
        self.session.flush()

    # ------------------------------------------------------------ resume

    def resume_after_user_input(self, job: Job) -> bool:
        """
        Release a paused job once its questions are answered.

        Returns False while anything is still outstanding, so a caller cannot
        accidentally resume an application that is still waiting.
        """
        if self.kb.pending_questions(job_id=job.id):
            return False
        move_job(self.session, job, JobStatus.READY_TO_APPLY, note="All questions answered")
        return True

    # ----------------------------------------------------------- helpers

    def _begin_attempt(self, job: Job) -> None:
        """
        Walk the job into APPLYING through the states the machine allows.

        A job arrives here MATCHED (or RETRY_PENDING, or released from
        WAITING_FOR_USER). Jumping straight to SUBMITTED or WAITING_FOR_USER
        would skip READY_TO_APPLY and APPLYING, and the state machine rejects
        it — correctly, because a crash mid-preparation must leave the job in
        APPLYING so the resumable-status sweep can find it.
        """
        if job.status in (JobStatus.MATCHED, JobStatus.RETRY_PENDING, JobStatus.WAITING_FOR_USER):
            move_job(self.session, job, JobStatus.READY_TO_APPLY)
        if job.status is JobStatus.READY_TO_APPLY:
            move_job(self.session, job, JobStatus.APPLYING)

    def _open_application(self, job: Job) -> Application:
        """Reuse the in-flight attempt if there is one, else start a new one."""
        existing = self.session.scalar(
            select(Application)
            .where(
                Application.job_id == job.id,
                Application.status.in_(
                    [
                        ApplicationStatus.DRAFT,
                        ApplicationStatus.PREPARED,
                        ApplicationStatus.WAITING_FOR_USER,
                    ]
                ),
            )
            .order_by(Application.created_at.desc())
        )
        if existing is not None:
            return existing

        attempts = self.session.scalar(
            select(Application).where(Application.job_id == job.id).order_by(Application.attempt.desc())
        )
        application = Application(
            job_id=job.id,
            company=job.company,
            job_title=job.title,
            source=job.source.slug if job.source else "",
            job_url=job.url,
            application_url=job.application_url,
            match_score=job.match_score,
            attempt=(attempts.attempt + 1) if attempts else 1,
            dry_run=self.settings.dry_run,
        )
        self.session.add(application)
        self.session.flush()
        log_event(
            self.session,
            EventType.APPLICATION_STARTED,
            job_id=job.id,
            application_id=application.id,
            message=f"Attempt {application.attempt} for {job.title} @ {job.company}",
        )
        return application

    def _select_resume(self, job: Job) -> ResumeVersion | None:
        """
        The resume to attach: tailored if warranted, master otherwise.

        Returns None rather than falling back to an unvalidated document —
        no resume is better than one that might misstate the candidate.
        """
        if self.resume_service is not None:
            version = self.resume_service.generate_for_job(job)
            if version is not None and version.validated:
                return version
            if version is not None and not version.validated:
                logger.warning(
                    "Resume %s is not validated; refusing to attach it", version.version_id
                )
            return None

        return self.session.scalar(
            select(ResumeVersion)
            .where(ResumeVersion.job_id == job.id, ResumeVersion.validated.is_(True))
            .order_by(ResumeVersion.created_at.desc())
        )

    def _company_cap_reached(self, job: Job) -> bool:
        """
        Whether we have already applied to this company too recently.

        Counted from the database rather than per-batch state, so the cap
        holds across consecutive runs instead of resetting each time.
        """
        from datetime import timedelta

        if not job.company:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.company_window_days)
        target = normalize_company(job.company)

        recent = self.session.scalars(
            select(Application).where(
                Application.status == ApplicationStatus.SUBMITTED,
                Application.submitted_at >= cutoff,
            )
        )
        count = sum(1 for a in recent if normalize_company(a.company) == target)
        return count >= self.settings.max_applications_per_company

    def _already_applied(self, job: Job) -> bool:
        return (
            self.session.scalar(
                select(Application).where(
                    Application.job_id == job.id,
                    Application.status == ApplicationStatus.SUBMITTED,
                )
            )
            is not None
        )
