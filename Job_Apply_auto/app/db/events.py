"""
Event logging and status transitions.

`move_job` is the only supported way to change a job's status: it validates
the move against the state machine and writes the matching event in the same
transaction. Doing both in one place is what makes the log trustworthy as a
restart record — there is no way to change state without leaving a trace.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import ApplicationEvent, EventType, Job, JobStatus, utcnow
from ..state import transition

logger = logging.getLogger(__name__)

#: Status a job lands in -> the event that records getting there. Only the
#: transitions with a natural event are listed; the rest pass `event_type`
#: explicitly or log none.
_STATUS_EVENTS: dict[JobStatus, EventType] = {
    JobStatus.MATCHED: EventType.JOB_MATCHED,
    JobStatus.REJECTED: EventType.JOB_REJECTED,
    JobStatus.APPLYING: EventType.APPLICATION_STARTED,
    JobStatus.WAITING_FOR_USER: EventType.USER_INPUT_REQUIRED,
    JobStatus.SUBMITTED: EventType.APPLICATION_SUBMITTED,
    JobStatus.FAILED: EventType.APPLICATION_FAILED,
    JobStatus.RETRY_PENDING: EventType.RETRY_STARTED,
}


def log_event(
    session: Session,
    event_type: EventType,
    *,
    job_id: int | None = None,
    application_id: int | None = None,
    message: str | None = None,
    payload: dict | None = None,
) -> ApplicationEvent:
    """Append one event. Never updates or deletes."""
    event = ApplicationEvent(
        job_id=job_id,
        application_id=application_id,
        event_type=event_type,
        message=message,
        payload=payload,
        created_at=utcnow(),
    )
    session.add(event)
    return event


def move_job(
    session: Session,
    job: Job,
    to_status: JobStatus,
    *,
    note: str | None = None,
    event_type: EventType | None = None,
    payload: dict | None = None,
) -> Job:
    """
    Move a job to `to_status`, validating the transition and logging it.

    Raises `IllegalTransition` if the move is not allowed. Callers should let
    that propagate: an illegal transition means the pipeline's model of where
    the job is has diverged from reality, and continuing would compound it.
    """
    previous = JobStatus(job.status)
    job.status = transition(previous, to_status)
    if note is not None:
        job.status_note = note

    if previous != job.status:
        chosen = event_type or _STATUS_EVENTS.get(job.status)
        if chosen is not None:
            log_event(
                session,
                chosen,
                job_id=job.id,
                message=note or f"{previous} -> {job.status}",
                payload=payload,
            )
        logger.debug("Job %s: %s -> %s", job.id, previous, job.status)
    return job


def last_event(session: Session, job_id: int) -> ApplicationEvent | None:
    """
    Most recent event for a job — where a restarted run should resume.
    """
    return (
        session.query(ApplicationEvent)
        .filter(ApplicationEvent.job_id == job_id)
        .order_by(ApplicationEvent.created_at.desc(), ApplicationEvent.id.desc())
        .first()
    )
