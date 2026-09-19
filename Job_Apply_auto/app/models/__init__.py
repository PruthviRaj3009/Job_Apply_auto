"""
ORM models for the unified platform.

Importing this package registers every mapper, so `Base.metadata` is complete.
Import models from here rather than from their individual modules — a partial
import leaves relationships unresolvable.
"""

from .application import Application, ApplicationError, ApplicationEvent
from .base import Base, TimestampMixin, utcnow
from .enums import (
    AnswerSource,
    ApplicationStatus,
    EmploymentType,
    EventType,
    JobStatus,
    QuestionCategory,
    ResumeKind,
    WorkMode,
)
from .job import Job, JobSource
from .profile import (
    Certification,
    Education,
    Experience,
    Profile,
    Project,
    Skill,
)
from .question import Answer, PendingQuestion, Question
from .resume import CoverLetter, ResumeVersion

__all__ = [
    "Answer",
    "AnswerSource",
    "Application",
    "ApplicationError",
    "ApplicationEvent",
    "ApplicationStatus",
    "Base",
    "Certification",
    "CoverLetter",
    "Education",
    "EmploymentType",
    "EventType",
    "Experience",
    "Job",
    "JobSource",
    "JobStatus",
    "PendingQuestion",
    "Profile",
    "Project",
    "Question",
    "QuestionCategory",
    "ResumeKind",
    "ResumeVersion",
    "Skill",
    "TimestampMixin",
    "WorkMode",
    "utcnow",
]
