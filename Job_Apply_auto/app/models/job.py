"""Job and JobSource models — the normalized shape every source maps into."""

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
from .enums import EmploymentType, JobStatus, WorkMode
from .types import EnumString


class JobSource(Base, TimestampMixin):
    """A configured origin of jobs: a portal, or one company's career page."""

    __tablename__ = "job_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Adapter key, e.g. "linkedin" or "careerpage:acme".
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="portal")
    base_url: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Per-source overrides (rate limits, selectors, search defaults).
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    jobs: Mapped[list["Job"]] = relationship(back_populates="source")

    def __repr__(self) -> str:
        return f"<JobSource {self.slug}>"


class Job(Base, TimestampMixin):
    """
    One posting, normalized from whichever source produced it.

    `fingerprint` is what makes "detect newly posted jobs" and "prevent
    duplicate processing" work: the same role cross-posted to three portals,
    or reposted next week under a new ID, collapses to one row. `url` alone is
    not enough — portals rewrite their URLs constantly.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_jobs_fingerprint"),
        Index("ix_jobs_status_score", "status", "match_score"),
        Index("ix_jobs_company", "company"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ------------------------------------------------------------- identity
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("job_sources.id"))
    #: The source's own ID for this posting, when it exposes one.
    external_id: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    application_url: Mapped[str | None] = mapped_column(String(1000))

    # --------------------------------------------------------------- detail
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    company: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    work_mode: Mapped[WorkMode] = mapped_column(
        EnumString(WorkMode, 16), default=WorkMode.UNKNOWN, nullable=False
    )
    employment_type: Mapped[EmploymentType] = mapped_column(
        EnumString(EmploymentType, 16), default=EmploymentType.UNKNOWN, nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text)
    salary_text: Mapped[str | None] = mapped_column(String(200))
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    experience_min_years: Mapped[float | None] = mapped_column(Float)
    experience_max_years: Mapped[float | None] = mapped_column(Float)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    easy_apply: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ------------------------------------------------- extracted by analysis
    skills: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    qualifications: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    responsibilities: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    mandatory_requirements: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    optional_requirements: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # -------------------------------------------------------------- matching
    match_score: Mapped[float | None] = mapped_column(Float)
    #: Full MatchResult payload: per-dimension scores, gaps, explanation.
    match_detail: Mapped[dict | None] = mapped_column(JSON)

    # --------------------------------------------------------------- status
    status: Mapped[JobStatus] = mapped_column(
        EnumString(JobStatus, 24), default=JobStatus.NEW, nullable=False, index=True
    )
    status_note: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Raw payload from the source, kept so a parser fix can be replayed
    #: without re-scraping.
    raw: Mapped[dict | None] = mapped_column(JSON)

    source: Mapped["JobSource | None"] = relationship(back_populates="jobs")
    applications: Mapped[list["Application"]] = relationship(  # noqa: F821
        back_populates="job", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.title!r} @ {self.company!r} [{self.status}]>"
