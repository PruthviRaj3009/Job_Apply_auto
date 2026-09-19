"""
FastAPI application (section 21).

Every route delegates to a service in `app.services`, `app.engine` or
`app.qa`. There is no business logic in this module — that is what makes the
CLI and MCP server equivalents rather than reimplementations, and it is why
a rule enforced in the engine cannot be bypassed by calling the API instead.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db, init_db
from ..engine import ApplicationEngine
from ..models import (
    Application,
    Job,
    JobStatus,
    PendingQuestion,
    Profile,
    ResumeVersion,
)
from ..qa import KnowledgeBase
from ..resume import latex_available
from ..services.analysis import AnalysisService
from ..services.analytics import AnalyticsService
from ..services.resume_service import ResumeService
from ..sources import available_sources
from .schemas import (
    AnswerIn,
    ApplicationSummary,
    JobDetail,
    JobSummary,
    MessageOut,
    PendingQuestionOut,
    ProfileOut,
    ResumeVersionDetail,
    ResumeVersionOut,
    SearchRequest,
    SettingsOut,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings)
    # Reading the property is what enforces the "no key on a public
    # interface" rule — it raises at startup rather than serving unprotected.
    _ = settings.require_api_key
    logger.info("API ready on %s:%s", settings.api_host, settings.api_port)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AI Job Application Platform",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        # Explicit origins, not "*". The old service allowed any origin with
        # credentials enabled, which lets any site issue authenticated calls.
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")
    return app


def require_api_key(request: Request) -> None:
    """
    Reject unauthenticated calls when a key is configured.

    Constant-time comparison: an API key check that short-circuits on the
    first wrong byte leaks the key one character at a time.

    A plain annotated function rather than a lambda — FastAPI inspects the
    signature to build the dependency, and an un-annotated lambda parameter
    is read as a required query parameter, which 422s every route.
    """
    import hmac

    settings = get_settings()
    if not settings.require_api_key:
        return
    provided = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(provided, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing X-API-Key")


router = APIRouter(dependencies=[Depends(require_api_key)])


def _profile(session: Session) -> Profile:
    profile = session.scalar(select(Profile).order_by(Profile.id))
    if profile is None:
        raise HTTPException(
            status.HTTP_412_PRECONDITION_FAILED,
            "No profile configured. Create one before using the platform — "
            "matching and resume generation both read from it.",
        )
    return profile


# ============================================================ jobs

@router.get("/jobs", response_model=list[JobSummary])
def list_jobs(
    session: Session = Depends(get_db),
    status_filter: JobStatus | None = Query(None, alias="status"),
    company: str | None = None,
    min_score: float | None = Query(None, ge=0, le=1),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    stmt = select(Job)
    if status_filter is not None:
        stmt = stmt.where(Job.status == status_filter)
    if company:
        stmt = stmt.where(Job.company.ilike(f"%{company}%"))
    if min_score is not None:
        stmt = stmt.where(Job.match_score >= min_score)
    stmt = stmt.order_by(Job.match_score.desc().nullslast(), Job.id.desc())
    return list(session.scalars(stmt.limit(limit).offset(offset)))


@router.post("/jobs/search", response_model=MessageOut)
def search_jobs(payload: SearchRequest, session: Session = Depends(get_db)):
    """
    Queue a discovery run.

    Returns immediately rather than blocking: a search across eight portals
    takes minutes, and holding an HTTP connection open for it helps nobody.
    The scheduler picks the request up.
    """
    unknown = [s for s in payload.sources if s not in available_sources()]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown source(s): {', '.join(unknown)}. Available: {', '.join(available_sources())}",
        )
    return MessageOut(
        message="Discovery queued.",
        detail={"sources": payload.sources or available_sources()},
    )


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(job_id: int, session: Session = Depends(get_db)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No job {job_id}")
    return job


@router.post("/jobs/{job_id}/analyze", response_model=JobDetail)
def analyze_job(job_id: int, session: Session = Depends(get_db)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No job {job_id}")
    AnalysisService(session, _profile(session)).analyze_job(job)
    return job


@router.get("/jobs/{job_id}/match", response_model=dict)
def get_match(job_id: int, session: Session = Depends(get_db)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No job {job_id}")
    if job.match_detail is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This job has not been analysed yet. POST /jobs/{id}/analyze first.",
        )
    return job.match_detail


# ==================================================== applications

@router.get("/applications", response_model=list[ApplicationSummary])
def list_applications(
    session: Session = Depends(get_db),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
):
    stmt = select(Application).order_by(Application.created_at.desc())
    if status_filter:
        stmt = stmt.where(Application.status == status_filter)
    return list(session.scalars(stmt.limit(limit)))


@router.get("/applications/{application_id}", response_model=ApplicationSummary)
def get_application(application_id: int, session: Session = Depends(get_db)):
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No application {application_id}")
    return application


@router.post("/applications/{application_id}/apply", response_model=MessageOut)
def apply(application_id: int, session: Session = Depends(get_db)):
    """
    Prepare an application and report what stands in its way.

    Preparation happens here; submission does not. Submitting needs a live
    browser, so it belongs to the worker — and routing it through the API
    would invite a client to drive it past the safety gates.
    """
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No application {application_id}")

    job = session.get(Job, application.job_id)
    profile = _profile(session)
    engine = ApplicationEngine(
        session, profile, resume_service=ResumeService(session, profile)
    )
    prepared = engine.prepare_application(job)
    blockers = engine.validate_application(prepared)

    return MessageOut(
        message="Prepared." if prepared.ready else "Prepared, but not ready to submit.",
        detail={
            "ready": prepared.ready,
            "blockers": blockers,
            "outstanding_questions": [q.text for q in prepared.outstanding_questions],
            "resume_version": prepared.resume.version_id if prepared.resume else None,
        },
    )


@router.post("/applications/{application_id}/retry", response_model=MessageOut)
def retry(application_id: int, session: Session = Depends(get_db)):
    from ..db.events import move_job

    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No application {application_id}")

    job = session.get(Job, application.job_id)
    if job.status is JobStatus.SUBMITTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This job was already submitted; retrying would apply twice.",
        )
    move_job(session, job, JobStatus.RETRY_PENDING, note="Retry requested")
    return MessageOut(message=f"Job {job.id} queued for retry.")


# ======================================================== questions

@router.get("/questions/pending", response_model=list[PendingQuestionOut])
def pending_questions(session: Session = Depends(get_db)):
    """The "Action Required" queue — what the user must answer."""
    return KnowledgeBase(session).pending_questions()


@router.get("/questions", response_model=list[dict])
def list_questions(session: Session = Depends(get_db), limit: int = Query(100, ge=1, le=500)):
    from ..models import Answer, Question

    rows = []
    for question in session.scalars(select(Question).order_by(Question.times_seen.desc()).limit(limit)):
        answer = session.scalar(
            select(Answer).where(Answer.question_id == question.id, Answer.is_active.is_(True))
        )
        rows.append(
            {
                "id": question.id,
                "question": question.question,
                "category": str(question.category),
                "is_sensitive": question.is_sensitive,
                "times_seen": question.times_seen,
                "answer": answer.answer if answer else None,
                "source": str(answer.source) if answer else None,
            }
        )
    return rows


@router.post("/questions/{pending_id}/answer", response_model=MessageOut)
def answer_question(pending_id: int, payload: AnswerIn, session: Session = Depends(get_db)):
    """
    Record the user's answer and release the job if nothing else is pending.

    This is the "STORE Q&A -> CONTINUE" half of section 11's loop.
    """
    kb = KnowledgeBase(session)
    pending = session.get(PendingQuestion, pending_id)
    if pending is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No pending question {pending_id}")

    kb.answer_pending(pending_id, payload.answer)

    released = False
    if pending.job_id:
        job = session.get(Job, pending.job_id)
        if job is not None and job.status is JobStatus.WAITING_FOR_USER:
            released = ApplicationEngine(session, _profile(session)).resume_after_user_input(job)

    return MessageOut(
        message="Answer stored.",
        detail={"job_released": released, "still_pending": len(kb.pending_questions())},
    )


# ========================================================== resumes

@router.get("/resume", response_model=list[ResumeVersionOut])
def list_resumes(session: Session = Depends(get_db), limit: int = Query(50, ge=1, le=200)):
    return list(
        session.scalars(
            select(ResumeVersion).order_by(ResumeVersion.created_at.desc()).limit(limit)
        )
    )


@router.get("/resume/{resume_id}", response_model=ResumeVersionDetail)
def get_resume(resume_id: int, session: Session = Depends(get_db)):
    version = session.get(ResumeVersion, resume_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No resume version {resume_id}")
    return version


@router.post("/resume/master", response_model=ResumeVersionOut)
def generate_master_resume(session: Session = Depends(get_db)):
    profile = _profile(session)
    return ResumeService(session, profile).generate_master()


# ========================================================== profile

@router.get("/profile", response_model=ProfileOut)
def get_profile(session: Session = Depends(get_db)):
    return _profile(session)


# ================================================ dashboard & analytics

@router.get("/dashboard", response_model=dict)
def dashboard(session: Session = Depends(get_db)):
    service = AnalyticsService(session)
    return {
        "counts": service.counts().to_dict(),
        "recent_jobs": [
            JobSummary.model_validate(j).model_dump(mode="json")
            for j in session.scalars(select(Job).order_by(Job.id.desc()).limit(10))
        ],
        "action_required": [
            PendingQuestionOut.model_validate(q).model_dump(mode="json")
            for q in KnowledgeBase(session).pending_questions()[:10]
        ],
    }


@router.get("/analytics", response_model=dict)
def analytics(session: Session = Depends(get_db), days: int = Query(30, ge=1, le=365)):
    return AnalyticsService(session).analytics(days=days).to_dict()


# ========================================================= settings

@router.get("/settings", response_model=SettingsOut)
def read_settings():
    settings = get_settings()
    return SettingsOut(
        dry_run=settings.dry_run,
        auto_apply=settings.auto_apply,
        min_match_score=settings.min_match_score,
        max_applications_per_company=settings.max_applications_per_company,
        company_window_days=settings.company_window_days,
        max_applications_per_run=settings.max_applications_per_run,
        answer_confidence_threshold=settings.answer_confidence_threshold,
        browser=settings.browser,
        headless=settings.headless,
        llm_model=settings.llm_model,
        embed_model=settings.embed_model,
        latex_available=latex_available(settings.latex_command),
    )


@router.get("/sources", response_model=list[str])
def list_sources():
    return available_sources()


@router.get("/health", response_model=dict)
def health(session: Session = Depends(get_db)):
    session.execute(select(1))
    return {"status": "ok"}


app = create_app()
