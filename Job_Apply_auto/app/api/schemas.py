"""
API request and response models.

Separate from the ORM on purpose: the wire format should be able to stay
stable while the schema moves, and nothing internal (raw payloads, compile
logs, tracebacks) should leak to a client by default.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..models.enums import ApplicationStatus, JobStatus


class JobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    company: str
    location: str
    url: str
    status: JobStatus
    match_score: float | None = None
    easy_apply: bool = False
    posted_at: datetime | None = None
    first_seen_at: datetime | None = None


class JobDetail(JobSummary):
    description: str | None = None
    application_url: str | None = None
    salary_text: str | None = None
    experience_min_years: float | None = None
    experience_max_years: float | None = None
    skills: list[str] = Field(default_factory=list)
    mandatory_requirements: list[str] = Field(default_factory=list)
    optional_requirements: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    match_detail: dict | None = None
    status_note: str | None = None


class ApplicationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    company: str
    job_title: str
    source: str
    status: ApplicationStatus
    match_score: float | None = None
    resume_version: str | None = None
    submitted_at: datetime | None = None
    error_message: str | None = None
    attempt: int = 1
    dry_run: bool = True
    pending_questions: list[str] = Field(default_factory=list)


class PendingQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int | None = None
    question_text: str
    field_type: str
    options: list[str] = Field(default_factory=list)
    is_sensitive: bool
    suggested_answer: str | None = None
    suggested_confidence: float | None = None
    created_at: datetime


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=5000)


class ResumeVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version_id: str
    kind: str
    job_id: int | None = None
    compiled: bool
    validated: bool
    page_count: int | None = None
    keywords_emphasized: list[str] = Field(default_factory=list)
    keywords_omitted: list[str] = Field(default_factory=list)
    tailoring_reason: str = ""
    generated_at: datetime | None = None


class ResumeVersionDetail(ResumeVersionOut):
    """
    Adds the audit trail.

    `supporting_evidence` is what makes the truthfulness claim checkable by a
    human: every emphasized keyword, and where in the profile it came from.
    """

    supporting_evidence: dict[str, str] = Field(default_factory=dict)
    validation_report: dict | None = None
    tex_path: str | None = None
    pdf_path: str | None = None


class SearchRequest(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    location: str = ""
    sources: list[str] = Field(default_factory=list)
    remote_only: bool = False
    max_days_old: int = Field(default=30, ge=1, le=365)
    easy_apply_only: bool = False
    limit: int = Field(default=100, ge=1, le=500)


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    email: str
    phone: str
    location: str
    headline: str
    total_experience_years: float
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    target_roles: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)


class SettingsOut(BaseModel):
    """
    Operational settings a client may see.

    Deliberately excludes api_key, database_url and credential paths: this
    endpoint exists so the dashboard can show whether auto-apply is on, not
    so it can enumerate the deployment.
    """

    dry_run: bool
    auto_apply: bool
    min_match_score: float
    max_applications_per_company: int
    company_window_days: int
    max_applications_per_run: int
    answer_confidence_threshold: float
    browser: str
    headless: bool
    llm_model: str
    embed_model: str
    latex_available: bool


class MessageOut(BaseModel):
    message: str
    detail: dict | None = None
