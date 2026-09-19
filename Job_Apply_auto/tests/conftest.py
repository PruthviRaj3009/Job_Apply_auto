"""Shared fixtures. Every test runs against a throwaway SQLite file."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import init_db, reset_engine, session_scope
from app.models import Job, JobSource, Profile, Skill


@pytest.fixture()
def settings(tmp_path, monkeypatch) -> Iterator[Settings]:
    """
    Settings pointed at a temp directory.

    A file-backed SQLite DB rather than :memory: — the engine is shared across
    sessions here, and in-memory databases vanish when the last connection to
    them closes, which `session_scope` does after every block.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("RESUME_OUTPUT_DIR", str(tmp_path / "generated"))
    monkeypatch.setenv("BROWSER_PROFILE_DIR", str(tmp_path / "profiles"))
    get_settings.cache_clear()
    reset_engine()

    s = get_settings()
    init_db(s)
    yield s

    reset_engine()
    get_settings.cache_clear()


@pytest.fixture()
def session(settings: Settings) -> Iterator[Session]:
    with session_scope(settings) as s:
        yield s


@pytest.fixture()
def source(session: Session) -> JobSource:
    src = JobSource(slug="testportal", display_name="Test Portal", kind="portal")
    session.add(src)
    session.flush()
    return src


@pytest.fixture()
def profile(session: Session) -> Profile:
    """A minimal but realistic profile, used by matching and resume tests."""
    p = Profile(
        full_name="Test Candidate",
        email="candidate@example.com",
        phone="+91-9000000000",
        location="Pune, India",
        headline="DevOps Engineer",
        total_experience_years=4.0,
        target_roles=["DevOps Engineer", "Platform Engineer"],
        preferred_locations=["Pune", "Remote", "Bangalore"],
        github_url="https://github.com/testcandidate",
        linkedin_url="https://linkedin.com/in/testcandidate",
    )
    session.add(p)
    session.flush()
    session.add_all(
        [
            Skill(profile_id=p.id, name="Kubernetes", years=3.0, evidence="Ran GKE clusters at Acme", aliases=["k8s"]),
            Skill(profile_id=p.id, name="Terraform", years=3.0, evidence="IaC for all Acme envs"),
            Skill(profile_id=p.id, name="Python", years=4.0, evidence="Automation tooling"),
            Skill(profile_id=p.id, name="GCP", years=3.0, evidence="Primary cloud at Acme"),
        ]
    )
    session.flush()
    return p


def make_job(session: Session, **kwargs) -> Job:
    """Create a Job with sensible defaults; override what the test cares about."""
    defaults = dict(
        fingerprint=f"fp-{kwargs.get('external_id', id(kwargs))}",
        url="https://example.com/job/1",
        title="DevOps Engineer",
        company="Acme",
        location="Pune",
    )
    defaults.update(kwargs)
    job = Job(**defaults)
    session.add(job)
    session.flush()
    return job
