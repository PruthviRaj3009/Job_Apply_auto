"""State machine: legal moves, rejected moves, and restart semantics."""

from __future__ import annotations

import pytest

from app.models import JobStatus
from app.state import (
    IllegalTransition,
    allowed_transitions,
    can_transition,
    transition,
)


def test_happy_path_to_submitted():
    """The full pipeline is reachable one legal step at a time."""
    status = JobStatus.NEW
    for nxt in (
        JobStatus.ANALYZING,
        JobStatus.MATCHED,
        JobStatus.READY_TO_APPLY,
        JobStatus.APPLYING,
        JobStatus.SUBMITTED,
    ):
        status = transition(status, nxt)
    assert status == JobStatus.SUBMITTED


def test_submitted_is_terminal():
    """Nothing follows a submission — it must not be re-applied to."""
    assert allowed_transitions(JobStatus.SUBMITTED) == frozenset()
    with pytest.raises(IllegalTransition):
        transition(JobStatus.SUBMITTED, JobStatus.APPLYING)


def test_rejected_is_terminal():
    assert allowed_transitions(JobStatus.REJECTED) == frozenset()
    with pytest.raises(IllegalTransition):
        transition(JobStatus.REJECTED, JobStatus.ANALYZING)


def test_cannot_skip_straight_from_new_to_applying():
    """A job must be matched before it can be applied to."""
    assert not can_transition(JobStatus.NEW, JobStatus.APPLYING)
    with pytest.raises(IllegalTransition):
        transition(JobStatus.NEW, JobStatus.APPLYING)


def test_waiting_for_user_resumes_into_applying():
    """Answering the outstanding question releases the job (section 11)."""
    assert can_transition(JobStatus.WAITING_FOR_USER, JobStatus.APPLYING)
    assert transition(JobStatus.WAITING_FOR_USER, JobStatus.APPLYING) == JobStatus.APPLYING


def test_failed_can_retry_but_not_resubmit_directly():
    assert can_transition(JobStatus.FAILED, JobStatus.RETRY_PENDING)
    assert not can_transition(JobStatus.FAILED, JobStatus.SUBMITTED)


def test_reentering_same_state_is_a_noop():
    """
    A retried step landing on its own status is not an error — otherwise
    every caller needs a bespoke idempotency guard.
    """
    assert transition(JobStatus.APPLYING, JobStatus.APPLYING) == JobStatus.APPLYING
    assert transition(JobStatus.SUBMITTED, JobStatus.SUBMITTED) == JobStatus.SUBMITTED


def test_every_status_has_a_transition_entry():
    """A status with no entry would raise KeyError deep inside a worker."""
    for status in JobStatus:
        assert isinstance(allowed_transitions(status), frozenset)


def test_error_message_names_the_legal_moves():
    """The exception has to be actionable when it surfaces in a log."""
    with pytest.raises(IllegalTransition) as exc:
        transition(JobStatus.NEW, JobStatus.SUBMITTED)
    assert "ANALYZING" in str(exc.value)
