"""
Job state machine.

Every status change goes through `transition()`. Centralising the legal moves
here is what makes the pipeline restartable: a process that dies mid-step
leaves a job in a known state, and the only way out of that state is a move
this table allows. Ad-hoc `job.status = ...` assignments elsewhere would make
"what can happen next" unanswerable.
"""

from __future__ import annotations

from .models.enums import JobStatus

#: status -> statuses reachable from it.
_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.NEW: frozenset({JobStatus.SEEN, JobStatus.ANALYZING, JobStatus.REJECTED}),
    JobStatus.SEEN: frozenset({JobStatus.ANALYZING, JobStatus.REJECTED}),
    JobStatus.ANALYZING: frozenset(
        {JobStatus.MATCHED, JobStatus.REJECTED, JobStatus.FAILED}
    ),
    JobStatus.MATCHED: frozenset(
        {JobStatus.READY_TO_APPLY, JobStatus.REJECTED, JobStatus.FAILED}
    ),
    JobStatus.READY_TO_APPLY: frozenset(
        {JobStatus.APPLYING, JobStatus.REJECTED, JobStatus.FAILED}
    ),
    JobStatus.APPLYING: frozenset(
        {
            JobStatus.WAITING_FOR_USER,
            JobStatus.SUBMITTED,
            JobStatus.FAILED,
            JobStatus.RETRY_PENDING,
        }
    ),
    # The user answered the outstanding question(s): pick the application back
    # up. It may also be abandoned outright.
    JobStatus.WAITING_FOR_USER: frozenset(
        {JobStatus.APPLYING, JobStatus.READY_TO_APPLY, JobStatus.REJECTED}
    ),
    JobStatus.FAILED: frozenset({JobStatus.RETRY_PENDING, JobStatus.REJECTED}),
    JobStatus.RETRY_PENDING: frozenset(
        {JobStatus.APPLYING, JobStatus.READY_TO_APPLY, JobStatus.REJECTED}
    ),
    # Terminal.
    JobStatus.SUBMITTED: frozenset(),
    JobStatus.REJECTED: frozenset(),
}


class IllegalTransition(Exception):
    """Raised when a caller tries to move a job to an unreachable state."""

    def __init__(self, current: JobStatus, requested: JobStatus) -> None:
        self.current = current
        self.requested = requested
        allowed = ", ".join(sorted(_ALLOWED[current])) or "(terminal)"
        super().__init__(
            f"Cannot move job from {current} to {requested}. Allowed: {allowed}"
        )


def allowed_transitions(current: JobStatus) -> frozenset[JobStatus]:
    """Statuses reachable from `current`."""
    return _ALLOWED[JobStatus(current)]


def can_transition(current: JobStatus, requested: JobStatus) -> bool:
    """True if `requested` is reachable from `current`."""
    return JobStatus(requested) in _ALLOWED[JobStatus(current)]


def transition(current: JobStatus, requested: JobStatus) -> JobStatus:
    """
    Validate a status change and return the new status.

    Re-entering the current state is allowed and is a no-op: a retried step
    that lands on the same status should not be an error, otherwise every
    caller needs its own idempotency check.
    """
    current = JobStatus(current)
    requested = JobStatus(requested)
    if current == requested:
        return current
    if requested not in _ALLOWED[current]:
        raise IllegalTransition(current, requested)
    return requested
