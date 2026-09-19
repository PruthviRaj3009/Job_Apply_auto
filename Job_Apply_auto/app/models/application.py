"""Application, ApplicationEvent and ApplicationError models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .enums import ApplicationStatus, EventType
from .types import EnumString


class Application(Base, TimestampMixin):
    """
    One attempt to apply to one job.

    A job can have several: a failed run, then a retry, then a submission. The
    resume and cover-letter versions used are recorded per attempt, so
    "which exact document did this employer receive?" is always answerable
    (section 9).
    """

    __tablename__ = "applications"
    __table_args__ = (Index("ix_applications_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Denormalized for reporting: Google Sheets and the dashboard read these
    # constantly, and a job row may later be pruned.
    company: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    job_title: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    source: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    job_url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    application_url: Mapped[str | None] = mapped_column(String(1000))
    match_score: Mapped[float | None] = mapped_column(Float)

    status: Mapped[ApplicationStatus] = mapped_column(
        EnumString(ApplicationStatus, 24), default=ApplicationStatus.DRAFT, nullable=False
    )
    resume_version: Mapped[str | None] = mapped_column(String(120))
    cover_letter_version: Mapped[str | None] = mapped_column(String(120))
    #: Whether this attempt ran with submission disabled.
    dry_run: Mapped[bool] = mapped_column(default=True, nullable=False)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Question/answer pairs actually submitted, for audit.
    answered_questions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    #: Questions that stopped this attempt, awaiting the user.
    pending_questions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    job: Mapped["Job"] = relationship(back_populates="applications")  # noqa: F821
    events: Mapped[list["ApplicationEvent"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Application {self.id} job={self.job_id} [{self.status}]>"


class ApplicationEvent(Base):
    """
    Append-only log of everything that happened, for audit and for restart.

    Never updated or deleted. After a crash the last event for a job tells the
    scheduler exactly which step to resume from.
    """

    __tablename__ = "application_events"
    __table_args__ = (Index("ix_events_job_created", "job_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[EventType] = mapped_column(EnumString(EventType, 40), nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    application: Mapped["Application | None"] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<Event {self.event_type} job={self.job_id}>"


class ApplicationError(Base, TimestampMixin):
    """
    A failure, captured in enough detail to diagnose without a re-run.

    One job failing must never take the batch down (section 30), so failures
    are recorded here and the loop continues.
    """

    __tablename__ = "application_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )
    stage: Mapped[str] = mapped_column(String(80), nullable=False)
    error_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    traceback: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(String(500))
    page_url: Mapped[str | None] = mapped_column(String(1000))
