"""
Application question/answer knowledge base (section 10, section 11).

Stores what the user has actually answered before, so the same question asked
by a different portal can be answered without bothering them again — but only
when the match is confident enough, and never for sensitive questions.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .enums import AnswerSource, QuestionCategory
from .types import EnumString


class Question(Base, TimestampMixin):
    """
    A question seen on an application form.

    `normalized_question` is the dedupe key: portals phrase the same question
    a dozen ways ("Years of experience with Python?", "How many years of
    Python experience do you have?"). Normalizing lets exact-match handle the
    common case before embeddings are consulted at all.
    """

    __tablename__ = "questions"
    __table_args__ = (
        UniqueConstraint("normalized_question", name="uq_questions_normalized"),
        Index("ix_questions_category", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[QuestionCategory] = mapped_column(
        EnumString(QuestionCategory, 24), default=QuestionCategory.OTHER, nullable=False
    )
    #: True when this question must always be confirmed by the user, whatever
    #: is stored (section 12).
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: "text" | "select" | "radio" | "checkbox" | "date" | "file"
    field_type: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    #: Options offered, when the question is a choice.
    options: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    times_seen: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Vector-store document id. The embedding itself lives in Chroma; keeping
    #: only the handle here avoids a second copy that can drift.
    embedding_id: Mapped[str | None] = mapped_column(String(120))

    answers: Mapped[list["Answer"]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Question {self.normalized_question[:50]!r}>"


class Answer(Base, TimestampMixin):
    """
    An answer the user gave, or one derived from their profile.

    Several answers can exist per question — a portal-specific phrasing, or a
    revision after the user corrected us. `is_active` picks the current one;
    superseded answers are kept rather than deleted so the audit trail for a
    submitted application stays intact.
    """

    __tablename__ = "answers"
    __table_args__ = (Index("ix_answers_question_active", "question_id", "is_active"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[AnswerSource] = mapped_column(
        EnumString(AnswerSource, 24), default=AnswerSource.USER, nullable=False
    )
    #: How much we trust this answer, 0..1. An answer the user typed is 1.0;
    #: one derived from the profile is lower and may be re-confirmed.
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Portal this answer was given for, when it is portal-specific.
    platform: Mapped[str | None] = mapped_column(String(80))
    times_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    question: Mapped["Question"] = relationship(back_populates="answers")

    def __repr__(self) -> str:
        return f"<Answer {self.answer[:40]!r} conf={self.confidence}>"


class PendingQuestion(Base, TimestampMixin):
    """
    A question that stopped an application and is waiting on the user.

    This is the queue behind the "Action Required" dashboard page. Resolving
    one writes an Answer and releases the job back into the pipeline.
    """

    __tablename__ = "pending_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id", ondelete="SET NULL"))
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    field_type: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    options: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Best stored candidate and its score, shown to the user as a suggestion.
    #: Present even for sensitive questions — suggesting is fine, auto-filling
    #: is not.
    suggested_answer: Mapped[str | None] = mapped_column(Text)
    suggested_confidence: Mapped[float | None] = mapped_column(Float)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<PendingQuestion {self.question_text[:40]!r} resolved={self.resolved}>"
