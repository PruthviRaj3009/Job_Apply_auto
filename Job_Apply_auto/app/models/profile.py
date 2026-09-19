"""
Master profile — the single truthful record of who the candidate is.

Everything downstream (matching, resume tailoring, answer resolution) reads
from here. Nothing writes claims into it except the user. That is the whole
basis of the truthfulness guarantee: if a skill is not in this table, no
generated resume may assert it and no answer may imply it (sections 7, 30).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


class Profile(Base, TimestampMixin):
    """The candidate. One row in normal single-user operation."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    location: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    headline: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Links preserved verbatim into every generated resume (sections 8, 30).
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    github_url: Mapped[str | None] = mapped_column(String(500))
    portfolio_url: Mapped[str | None] = mapped_column(String(500))
    other_links: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    total_experience_years: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    notice_period_days: Mapped[int | None] = mapped_column(Integer)
    current_ctc: Mapped[str | None] = mapped_column(String(80))
    expected_ctc: Mapped[str | None] = mapped_column(String(80))

    # ------------------------------------------------------- search prefs
    target_roles: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    search_keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    preferred_locations: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    excluded_keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    excluded_companies: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    experiences: Mapped[list["Experience"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", order_by="Experience.sort_order"
    )
    educations: Mapped[list["Education"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", order_by="Education.sort_order"
    )
    projects: Mapped[list["Project"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", order_by="Project.sort_order"
    )
    skills: Mapped[list["Skill"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    certifications: Mapped[list["Certification"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", order_by="Certification.sort_order"
    )

    def __repr__(self) -> str:
        return f"<Profile {self.full_name!r}>"


class Skill(Base, TimestampMixin):
    """
    A skill the candidate actually has.

    `evidence` and `years` are what let the resume generator decide whether a
    job's keyword may be *emphasized* (it is genuinely supported) or must be
    left out (it is not). See `app.resume.gap_analysis`.
    """

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    years: Mapped[float | None] = mapped_column(Float)
    proficiency: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    #: Where this skill was actually used — free text referencing real work.
    evidence: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Alternate spellings a JD might use ("k8s" for "Kubernetes").
    aliases: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    profile: Mapped["Profile"] = relationship(back_populates="skills")

    def __repr__(self) -> str:
        return f"<Skill {self.name!r}>"


class Experience(Base, TimestampMixin):
    __tablename__ = "experiences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    location: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_internship: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Achievement bullets, verbatim. The generator selects and orders these;
    #: it never rewrites them into claims the user did not make.
    bullets: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    technologies: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    profile: Mapped["Profile"] = relationship(back_populates="experiences")


class Education(Base, TimestampMixin):
    __tablename__ = "educations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    institution: Mapped[str] = mapped_column(String(300), nullable=False)
    degree: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    field_of_study: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    grade: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    profile: Mapped["Profile"] = relationship(back_populates="educations")


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    bullets: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    technologies: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    #: Real project URL. Carried into every resume unchanged (section 30).
    url: Mapped[str | None] = mapped_column(String(500))
    repo_url: Mapped[str | None] = mapped_column(String(500))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    profile: Mapped["Profile"] = relationship(back_populates="projects")


class Certification(Base, TimestampMixin):
    __tablename__ = "certifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(250), nullable=False)
    issuer: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    issued_date: Mapped[date | None] = mapped_column(Date)
    expires_date: Mapped[date | None] = mapped_column(Date)
    credential_id: Mapped[str | None] = mapped_column(String(200))
    credential_url: Mapped[str | None] = mapped_column(String(500))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    profile: Mapped["Profile"] = relationship(back_populates="certifications")
