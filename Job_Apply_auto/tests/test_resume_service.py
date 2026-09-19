"""Resume service: the tailor-or-not decision, versioning, and refusal."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import EventType, ResumeKind, ResumeVersion
from app.services.resume_service import ResumeService

from .conftest import make_job
from .test_resume import rich_profile  # noqa: F401 - fixture


@pytest.fixture()
def thin_coverage_profile(session, rich_profile):  # noqa: F811
    """
    A profile holding skills the master resume barely mentions.

    rich_profile's headline skills appear several times over in the generated
    master (skills list, role technologies, project stack, certification), so
    they are correctly judged well represented and need no tailoring. Real
    tailoring cases are skills the profile genuinely has but the resume states
    thinly — which is what these add.
    """
    from app.models import Skill

    session.add_all(
        [
            Skill(profile_id=rich_profile.id, name="Kafka", years=1.0,
                  evidence="Event pipeline on the autoscaler project"),
            Skill(profile_id=rich_profile.id, name="Redis", years=2.0,
                  evidence="Caching layer at Acme"),
            Skill(profile_id=rich_profile.id, name="PostgreSQL", years=3.0,
                  evidence="Primary datastore at Acme"),
        ]
    )
    session.flush()
    session.refresh(rich_profile)
    return rich_profile


def test_master_resume_is_generated_and_versioned(session, rich_profile, settings):
    svc = ResumeService(session, rich_profile, settings=settings)
    master = svc.generate_master()

    assert master.version_id == "resume_master_v1"
    assert master.kind is ResumeKind.MASTER_RESUME
    assert Path(master.tex_path).exists()


def test_master_versions_increment(session, rich_profile, settings):
    svc = ResumeService(session, rich_profile, settings=settings)
    svc.generate_master()
    assert svc.generate_master().version_id == "resume_master_v2"


def test_job_specific_version_id_is_readable(session, thin_coverage_profile, settings):
    """
    The id appears in the tracking sheet and in filenames the user opens by
    hand, so it has to be legible.
    """
    svc = ResumeService(session, thin_coverage_profile, settings=settings)
    svc.generate_master()
    job = make_job(
        session, fingerprint="v1", title="Senior DevOps Engineer",
        skills=["Kafka", "Redis", "PostgreSQL"],
    )
    version = svc.generate_for_job(job)

    assert version is not None
    assert version.version_id.startswith("resume_senior_devops_engineer_")
    assert version.version_id.endswith("_v1")


def test_tailoring_is_skipped_when_nothing_to_surface(session, rich_profile, settings):
    """
    Generating a near-identical variant just adds a version to track, so the
    service returns the master instead.
    """
    svc = ResumeService(session, rich_profile, settings=settings)
    master = svc.generate_master()
    job = make_job(session, fingerprint="v2", title="DevOps Engineer", skills=["Salesforce"])

    assert svc.decide(job).tailor is False
    assert svc.generate_for_job(job).id == master.id


def test_tailoring_happens_when_there_are_real_gaps(session, thin_coverage_profile, settings):
    svc = ResumeService(session, thin_coverage_profile, settings=settings)
    svc.generate_master()
    job = make_job(
        session, fingerprint="v3", title="Platform Engineer",
        skills=["Kafka", "Redis", "PostgreSQL"],
    )
    decision = svc.decide(job)

    assert decision.tailor is True
    assert "surfacing" in decision.reason or "promoting" in decision.reason


def test_generated_version_records_its_evidence(session, thin_coverage_profile, settings):
    """
    The audit trail for the truthfulness rule: every emphasized keyword must
    carry the profile evidence that cleared it.
    """
    svc = ResumeService(session, thin_coverage_profile, settings=settings)
    svc.generate_master()
    job = make_job(
        session, fingerprint="v4", title="DevOps Engineer",
        skills=["Kafka", "Redis", "PostgreSQL", "Salesforce"],
    )
    version = svc.generate_for_job(job)

    assert version is not None
    assert "Salesforce" in version.keywords_omitted
    for keyword in version.keywords_emphasized:
        assert version.supporting_evidence[keyword]


def test_tailoring_logs_its_events(session, thin_coverage_profile, settings):
    from app.models import ApplicationEvent

    svc = ResumeService(session, thin_coverage_profile, settings=settings)
    svc.generate_master()
    job = make_job(
        session, fingerprint="v5", title="DevOps Engineer",
        skills=["Kafka", "Redis", "PostgreSQL"],
    )
    svc.generate_for_job(job)
    session.flush()

    types = {
        e.event_type
        for e in session.query(ApplicationEvent).filter_by(job_id=job.id).all()
    }
    assert EventType.RESUME_TAILORING_STARTED in types
    assert EventType.RESUME_GENERATED in types


def test_a_resume_failing_source_validation_is_never_returned(
    session, thin_coverage_profile, settings, monkeypatch
):
    """
    A resume asserting something unsupported must not be usable, whatever
    else is true of it — the engine can then refuse to attach anything.
    """
    svc = ResumeService(session, thin_coverage_profile, settings=settings)
    svc.generate_master()
    job = make_job(
        session, fingerprint="v6", title="DevOps Engineer",
        skills=["Kafka", "Redis", "PostgreSQL", "Salesforce"],
    )

    real_generate = svc.generator.generate

    def smuggle(profile, **kwargs):
        result = real_generate(profile, **kwargs)
        result.tex = result.tex.replace(
            "\\section{Skills}", "\\section{Skills}\nSalesforce architect."
        )
        return result

    svc.generator.generate = smuggle
    assert svc.generate_for_job(job) is None

    stored = session.query(ResumeVersion).filter_by(job_id=job.id).one()
    assert stored.validated is False
    assert any("Salesforce" in e for e in stored.validation_report["errors"])


def test_missing_latex_toolchain_is_a_warning_not_a_failure(
    session, thin_coverage_profile, settings, monkeypatch
):
    """
    Not having pdflatex installed is a real limitation, but the .tex is still
    produced and the source-level truthfulness checks still run.
    """
    monkeypatch.setattr("app.services.resume_service.latex_available", lambda cmd: False)
    svc = ResumeService(session, thin_coverage_profile, settings=settings)
    svc.generate_master()
    job = make_job(
        session, fingerprint="v7", title="DevOps Engineer",
        skills=["Kafka", "Redis", "PostgreSQL"],
    )
    version = svc.generate_for_job(job)

    assert version is not None
    assert Path(version.tex_path).exists()
    assert version.compiled is False
    assert version.validated is False  # no PDF, so not fully validated
    assert any("not installed" in w for w in version.validation_report["warnings"])


def test_job_keywords_come_from_stored_analysis(session, rich_profile, settings):
    """Matching already did the extraction; tailoring reuses it."""
    svc = ResumeService(session, rich_profile, settings=settings)
    job = make_job(
        session, fingerprint="v8", title="DevOps Engineer", skills=["Kubernetes"],
        match_detail={"missing_skills": ["Kafka"], "matching_skills": ["Terraform"]},
    )
    assert set(ResumeService._job_keywords(job)) == {"Kubernetes", "Kafka", "Terraform"}


def test_job_with_no_keywords_is_not_tailored(session, rich_profile, settings):
    svc = ResumeService(session, rich_profile, settings=settings)
    job = make_job(session, fingerprint="v9", title="DevOps Engineer")
    decision = svc.decide(job)

    assert decision.tailor is False
    assert "No keywords" in decision.reason


@pytest.mark.latex
def test_compiles_to_pdf_when_toolchain_present(session, rich_profile, settings):
    """
    Skipped unless a LaTeX distribution is installed. Run with
    `pytest -m latex` on a machine that has pdflatex.
    """
    from app.resume import latex_available

    if not latex_available(settings.latex_command):
        pytest.skip("pdflatex not installed")

    svc = ResumeService(session, rich_profile, settings=settings)
    master = svc.generate_master()

    assert master.compiled is True
    assert master.pdf_path and Path(master.pdf_path).exists()
    assert master.page_count and master.page_count >= 1
    assert master.validated is True
