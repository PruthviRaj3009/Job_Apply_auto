"""
Enumerations shared across the platform.

The job lifecycle (`JobStatus`) is the spine of the system: discovery writes
NEW, matching moves jobs to MATCHED or REJECTED, and the application engine
drives the rest. Legal transitions live in `app.state`, not here, so the set
of states stays readable on its own.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle of a single discovered job."""

    NEW = "NEW"
    SEEN = "SEEN"
    ANALYZING = "ANALYZING"
    MATCHED = "MATCHED"
    REJECTED = "REJECTED"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLYING = "APPLYING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    RETRY_PENDING = "RETRY_PENDING"


#: States from which no further automated work happens without a new trigger.
TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.REJECTED, JobStatus.SUBMITTED}
)

#: States a crashed run can be safely resumed from. APPLYING and ANALYZING are
#: included deliberately: a process that died mid-step left the job parked in
#: one of them, and leaving it there would strand the job forever.
RESUMABLE_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.ANALYZING,
        JobStatus.APPLYING,
        JobStatus.RETRY_PENDING,
    }
)


class ApplicationStatus(StrEnum):
    """Lifecycle of an application attempt against a job."""

    DRAFT = "DRAFT"
    PREPARED = "PREPARED"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    # Post-submission signals, set by the Gmail processor or by the user.
    VIEWED = "VIEWED"
    RESPONDED = "RESPONDED"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"
    REJECTED = "REJECTED"


class EventType(StrEnum):
    """Append-only audit trail. Every meaningful transition writes one."""

    JOB_DISCOVERED = "JOB_DISCOVERED"
    JOB_ANALYZED = "JOB_ANALYZED"
    JOB_MATCHED = "JOB_MATCHED"
    JOB_REJECTED = "JOB_REJECTED"
    RESUME_TAILORING_STARTED = "RESUME_TAILORING_STARTED"
    RESUME_GENERATED = "RESUME_GENERATED"
    RESUME_COMPILED = "RESUME_COMPILED"
    APPLICATION_STARTED = "APPLICATION_STARTED"
    QUESTION_FOUND = "QUESTION_FOUND"
    USER_INPUT_REQUIRED = "USER_INPUT_REQUIRED"
    ANSWER_RETRIEVED = "ANSWER_RETRIEVED"
    RESUME_UPLOADED = "RESUME_UPLOADED"
    FORM_FILLED = "FORM_FILLED"
    APPLICATION_SUBMITTED = "APPLICATION_SUBMITTED"
    APPLICATION_FAILED = "APPLICATION_FAILED"
    RETRY_STARTED = "RETRY_STARTED"


class QuestionCategory(StrEnum):
    """
    What kind of thing an application question is asking for.

    `SENSITIVE` is not a topic but a policy flag: those questions always go
    back to the user for explicit confirmation, even when a confident stored
    answer exists. See `app.qa.sensitive`.
    """

    PERSONAL = "PERSONAL"
    EXPERIENCE = "EXPERIENCE"
    COMPENSATION = "COMPENSATION"
    AVAILABILITY = "AVAILABILITY"
    SKILLS = "SKILLS"
    EDUCATION = "EDUCATION"
    LOGISTICS = "LOGISTICS"
    SENSITIVE = "SENSITIVE"
    OTHER = "OTHER"


class AnswerSource(StrEnum):
    """Where an answer came from — recorded so provenance is auditable."""

    USER = "USER"
    PROFILE = "PROFILE"
    KNOWLEDGE_BASE = "KNOWLEDGE_BASE"
    DERIVED = "DERIVED"


class ResumeKind(StrEnum):
    MASTER_RESUME = "MASTER_RESUME"
    JOB_SPECIFIC_RESUME = "JOB_SPECIFIC_RESUME"


class WorkMode(StrEnum):
    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ONSITE = "ONSITE"
    UNKNOWN = "UNKNOWN"


class EmploymentType(StrEnum):
    FULL_TIME = "FULL_TIME"
    PART_TIME = "PART_TIME"
    CONTRACT = "CONTRACT"
    INTERNSHIP = "INTERNSHIP"
    TEMPORARY = "TEMPORARY"
    UNKNOWN = "UNKNOWN"
