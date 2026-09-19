"""JD parsing and matching: structure extraction, scoring, and not-guessing."""

from __future__ import annotations

import pytest

from app.analysis import extract_skills, is_mandatory, parse_jd
from app.matching import DimensionVerdict, MatchingEngine, ProfileSnapshot
from app.models.enums import WorkMode

from .conftest import make_job

FULL_JD = """
About the role
We are hiring a DevOps Engineer to own our platform.

Responsibilities:
- Own our Kubernetes platform across GCP
- Build and maintain CI/CD pipelines
- Improve observability with Prometheus and Grafana

Requirements:
- Must have 3+ years of experience with Kubernetes
- Terraform is required
- Strong Python scripting skills required

Nice to have:
- Experience with Datadog is a plus
- Exposure to LLM tooling would be great

Benefits:
- Health insurance
- Annual bonus
"""


# ------------------------------------------------------------ skill extraction

def test_extracts_canonical_skills():
    skills = extract_skills("We use Kubernetes, k8s tooling, Terraform and GCP.")
    assert "Kubernetes" in skills
    assert "Terraform" in skills
    assert "GCP" in skills
    # Aliases collapse to one canonical entry.
    assert skills.count("Kubernetes") == 1


def test_short_skill_names_do_not_match_inside_words():
    """
    The classic failure: "Go" matching "Google", "js" matching "jsonify".
    A word-boundary miss here silently poisons every match score.
    """
    assert "Go" not in extract_skills("Google is a great company")
    assert "Go" in extract_skills("Strong golang experience")
    assert "Java" not in extract_skills("We write JavaScript daily")


def test_slash_bearing_skills_match():
    assert "CI/CD" in extract_skills("Own our CI/CD pipelines")


def test_unknown_technologies_are_not_invented():
    """
    Only vocabulary skills are returned. Harvesting arbitrary capitalized
    tokens would turn company names and headings into phantom requirements.
    """
    assert extract_skills("Experience with Zorblax and Framistan") == []


def test_extract_skills_on_empty_text():
    assert extract_skills("") == []


# --------------------------------------------------------------- JD structure

def test_parses_sections():
    parsed = parse_jd(FULL_JD)
    assert any("Kubernetes platform" in r for r in parsed.responsibilities)
    assert any("3+ years" in r for r in parsed.mandatory_requirements)
    assert any("Datadog" in r for r in parsed.optional_requirements)


def test_benefits_are_not_treated_as_requirements():
    parsed = parse_jd(FULL_JD)
    everything = " ".join(
        parsed.mandatory_requirements + parsed.optional_requirements + parsed.responsibilities
    )
    assert "Health insurance" not in everything
    assert "Annual bonus" not in everything


def test_optional_markers_beat_mandatory_ones():
    """
    "Preferred but not required" is optional. Scanning for "required" first
    would classify it as a hard requirement and wrongly sink the score.
    """
    assert is_mandatory("Kubernetes is required") is True
    assert is_mandatory("Kubernetes preferred but not required") is False
    assert is_mandatory("AWS certification is a plus") is False
    assert is_mandatory("Must have 5 years of Java") is True


def test_parses_inline_bullets():
    """Many portals deliver a JD as one paragraph with inline bullets."""
    parsed = parse_jd("Requirements: • Must have Python • Must have Docker")
    assert len(parsed.mandatory_requirements) == 2


def test_empty_jd_yields_empty_structure():
    parsed = parse_jd("")
    assert parsed.skills == []
    assert parsed.mandatory_requirements == []


def test_unstructured_jd_still_yields_skills():
    """A JD with no headings at all must not come back completely empty."""
    parsed = parse_jd("Looking for someone strong in Kubernetes and Terraform.")
    assert "Kubernetes" in parsed.skills


# -------------------------------------------------------------------- matching

@pytest.fixture()
def engine(profile) -> MatchingEngine:
    return MatchingEngine(profile)


def test_strong_match_scores_high(session, engine):
    job = make_job(
        session,
        fingerprint="strong",
        title="DevOps Engineer",
        location="Pune",
        description=FULL_JD,
        experience_min_years=3,
    )
    result = engine.match(job)

    assert result.match_score >= 0.7
    assert "Kubernetes" in result.matching_skills
    assert "Terraform" in result.matching_skills
    assert result.role_match.verdict is DimensionVerdict.MATCH


def test_off_target_role_scores_low(session, engine):
    job = make_job(
        session,
        fingerprint="offtarget",
        title="Frontend React Developer",
        location="Pune",
        description="Requirements:\n- Must have React and TypeScript",
    )
    result = engine.match(job)

    assert result.match_score < 0.5
    assert result.role_match.verdict is DimensionVerdict.MISMATCH


def test_missing_mandatory_skills_are_separated_from_nice_to_haves(session, engine):
    job = make_job(
        session,
        fingerprint="mandatory",
        title="DevOps Engineer",
        description=(
            "Requirements:\n- Must have Kafka experience\n"
            "Nice to have:\n- Datadog is a plus\n"
        ),
    )
    result = engine.match(job)

    assert "Kafka" in result.missing_mandatory_skills
    # Datadog is missing too, but only as a nice-to-have.
    assert "Datadog" in result.missing_skills
    assert "Datadog" not in result.missing_mandatory_skills


def test_mandatory_misses_reduce_the_score(session, engine):
    without = make_job(
        session, fingerprint="m1", title="DevOps Engineer",
        description="Requirements:\n- Must have Kubernetes\n- Terraform required",
    )
    with_misses = make_job(
        session, fingerprint="m2", title="DevOps Engineer",
        description=(
            "Requirements:\n- Must have Kubernetes\n- Terraform required\n"
            "- Kafka is required\n- Snowflake is required\n"
        ),
    )
    assert engine.match(with_misses).match_score < engine.match(without).match_score


def test_silent_jd_fields_are_unknown_not_mismatched(session, engine):
    """
    A JD that says nothing about education or experience has not failed those
    checks. Treating silence as failure would reject most real postings.
    """
    job = make_job(
        session, fingerprint="silent", title="DevOps Engineer",
        description="Join our team and do great work.",
    )
    result = engine.match(job)

    assert result.education_match.verdict is DimensionVerdict.UNKNOWN
    assert result.experience_match.verdict is DimensionVerdict.UNKNOWN


def test_experience_gap_within_a_year_is_partial_not_a_mismatch(session, engine):
    """A 4-year candidate is a reasonable applicant for a 5-year posting."""
    job = make_job(
        session, fingerprint="exp", title="DevOps Engineer", experience_min_years=5,
    )
    assert engine.match(job).experience_match.verdict is DimensionVerdict.PARTIAL


def test_large_experience_gap_is_a_mismatch(session, engine):
    job = make_job(
        session, fingerprint="exp2", title="DevOps Engineer", experience_min_years=12,
    )
    assert engine.match(job).experience_match.verdict is DimensionVerdict.MISMATCH


def test_remote_job_matches_when_remote_is_acceptable(session, engine):
    job = make_job(
        session, fingerprint="rem", title="DevOps Engineer",
        location="Remote", work_mode=WorkMode.REMOTE,
    )
    assert engine.match(job).location_match.verdict is DimensionVerdict.MATCH


def test_unpreferred_city_is_a_location_mismatch(session, engine):
    job = make_job(
        session, fingerprint="loc", title="DevOps Engineer", location="Chennai",
    )
    assert engine.match(job).location_match.verdict is DimensionVerdict.MISMATCH


def test_masters_satisfies_a_bachelors_requirement(session, profile):
    from app.models import Education

    session.add(
        Education(profile_id=profile.id, institution="Test University",
                  degree="Master of Technology", field_of_study="Computer Science")
    )
    session.flush()
    session.refresh(profile)

    job = make_job(
        session, fingerprint="edu", title="DevOps Engineer",
        description="Requirements:\n- Bachelor's degree in Computer Science required",
    )
    result = MatchingEngine(profile).match(job)
    assert result.education_match.verdict is DimensionVerdict.MATCH


def test_explanation_is_populated(session, engine):
    job = make_job(session, fingerprint="expl", title="DevOps Engineer", description=FULL_JD)
    result = engine.match(job)

    assert result.match_explanation
    assert "DevOps Engineer" in result.match_explanation
    assert f"{result.match_score:.0%}" in result.match_explanation


def test_missing_description_is_raised_as_a_concern(session, engine):
    """The user should know a score came from the title alone."""
    job = make_job(session, fingerprint="nodesc", title="DevOps Engineer", description=None)
    concerns = " ".join(engine.match(job).concerns).lower()
    assert "description" in concerns


def test_result_is_serializable(session, engine):
    """The whole result is stored in Job.match_detail as JSON."""
    job = make_job(session, fingerprint="ser", title="DevOps Engineer", description=FULL_JD)
    data = engine.match(job).to_dict()

    import json

    json.dumps(data)  # must not raise
    assert set(data) >= {
        "match_score", "matching_skills", "missing_skills", "experience_match",
        "education_match", "location_match", "role_match", "preference_match",
        "concerns", "match_explanation",
    }


def test_signals_record_which_scorers_ran(session, engine):
    """
    A run without Ollama must be distinguishable from one where semantic
    scoring ran and found nothing.
    """
    job = make_job(session, fingerprint="sig", title="DevOps Engineer", description=FULL_JD)
    assert engine.match(job).signals_used == ["rules"]


def test_semantic_scorer_blends_but_does_not_dominate(session, profile):
    """
    A confidently wrong embedding must not on its own promote an unsuitable
    job — the deterministic score stays the anchor.
    """
    class AlwaysPerfect:
        def similarity(self, job):
            return 1.0

    job = make_job(
        session, fingerprint="sem", title="Frontend React Developer",
        description="Requirements:\n- Must have React",
    )
    plain = MatchingEngine(profile).match(job).match_score
    boosted = MatchingEngine(profile, semantic=AlwaysPerfect()).match(job).match_score

    assert boosted > plain
    assert boosted < 1.0
    assert "embeddings" in MatchingEngine(profile, semantic=AlwaysPerfect()).match(job).signals_used


def test_broken_semantic_scorer_does_not_break_matching(session, profile):
    """RAG being down must degrade the score's precision, not the pipeline."""
    class Broken:
        def similarity(self, job):
            raise RuntimeError("ollama not running")

    job = make_job(session, fingerprint="broken", title="DevOps Engineer", description=FULL_JD)
    result = MatchingEngine(profile, semantic=Broken()).match(job)

    assert result.match_score > 0
    assert result.signals_used == ["rules"]
