"""
Resume and cover-letter versioning (sections 8, 9).

Every generated document is immutable and addressable by version id, and
records *why* it was tailored. That record is the audit trail for the
truthfulness rule: it must always be possible to show that every keyword
added to a resume was already supported by the master profile.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .enums import ResumeKind
from .types import EnumString


class ResumeVersion(Base, TimestampMixin):
    """
    One generated resume: its LaTeX source, its PDF, and its provenance.

    `version_id` is human-readable by design — e.g.
    `resume_python_backend_2026_09_18_v1` — because it appears in the tracking
    sheet and in filenames the user will open by hand.
    """

    __tablename__ = "resume_versions"
    __table_args__ = (UniqueConstraint("version_id", name="uq_resume_version_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[ResumeKind] = mapped_column(
        EnumString(ResumeKind, 32), default=ResumeKind.JOB_SPECIFIC_RESUME, nullable=False
    )
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))
    #: Null for the master resume, which is not tied to any one job.
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)

    tex_path: Mapped[str | None] = mapped_column(String(600))
    pdf_path: Mapped[str | None] = mapped_column(String(600))
    template: Mapped[str] = mapped_column(String(120), default="base_resume", nullable=False)

    # ------------------------------------------------------- tailoring record
    #: Keywords emphasized or surfaced for this job. Every one of these was
    #: already present in the master profile — see `supporting_evidence`.
    keywords_emphasized: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    #: JD keywords deliberately NOT added because the profile does not support
    #: them. Kept so the user can see what a job wanted that they lack.
    keywords_omitted: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    #: keyword -> where in the profile it came from.
    supporting_evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    tailoring_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # ------------------------------------------------------------ validation
    compiled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    validated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    validation_report: Mapped[dict | None] = mapped_column(JSON)
    compile_log: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cover_letters: Mapped[list["CoverLetter"]] = relationship(back_populates="resume_version")

    def __repr__(self) -> str:
        return f"<ResumeVersion {self.version_id} validated={self.validated}>"


class CoverLetter(Base, TimestampMixin):
    """A generated cover letter, versioned alongside the resume it went with."""

    __tablename__ = "cover_letters"
    __table_args__ = (UniqueConstraint("version_id", name="uq_cover_letter_version_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[str] = mapped_column(String(120), nullable=False)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    resume_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_versions.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    pdf_path: Mapped[str | None] = mapped_column(String(600))

    resume_version: Mapped["ResumeVersion | None"] = relationship(back_populates="cover_letters")
