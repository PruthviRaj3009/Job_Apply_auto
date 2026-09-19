"""
Resume generation — above all, that it cannot lie.

The truthfulness rule (sections 7, 30) is the reason this subsystem exists,
so most of these tests are attempts to get an unsupported claim onto a
resume and confirm that the design refuses.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.models import Certification, Education, Experience, Project, Skill
from app.resume import (
    GapAnalyzer,
    KeywordStatus,
    ResumeGenerator,
    validate_tex,
)
from app.resume.latex import escape, escape_url, format_date_range, link, section


@pytest.fixture()
def rich_profile(session, profile):
    """A profile with real work history, projects and certifications."""
    session.add_all(
        [
            Experience(
                profile_id=profile.id, company="Acme Corp", title="DevOps Engineer",
                location="Pune", start_date=date(2022, 1, 1), is_current=True,
                bullets=[
                    "Ran production GKE clusters serving 40M requests/day",
                    "Cut deployment time 60% with GitHub Actions pipelines",
                    "Owned Terraform modules for all environments",
                ],
                technologies=["Kubernetes", "Terraform", "GCP", "GitHub Actions"],
            ),
            Experience(
                profile_id=profile.id, company="Globex", title="Junior SRE",
                start_date=date(2020, 6, 1), end_date=date(2021, 12, 31),
                bullets=["Maintained Prometheus and Grafana dashboards"],
                technologies=["Prometheus", "Grafana"],
            ),
            Education(
                profile_id=profile.id, institution="Pune University",
                degree="Bachelor of Engineering", field_of_study="Computer Science",
                start_date=date(2016, 8, 1), end_date=date(2020, 5, 1),
            ),
            Project(
                profile_id=profile.id, name="Cluster Autoscaler",
                description="Custom autoscaler for GKE workloads",
                bullets=["Reduced idle node cost by 35%"],
                technologies=["Go", "Kubernetes"],
                url="https://example.com/autoscaler",
                repo_url="https://github.com/testcandidate/autoscaler",
            ),
            Certification(
                profile_id=profile.id, name="Certified Kubernetes Administrator",
                issuer="CNCF", issued_date=date(2023, 3, 1),
                credential_url="https://cert.example.com/cka/123",
            ),
        ]
    )
    session.flush()
    session.refresh(profile)
    return profile


# ============================================================== LaTeX escaping

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("C# & .NET", r"C\# \& .NET"),
        ("20% faster", r"20\% faster"),
        ("R&D", r"R\&D"),
        ("salary_range", r"salary\_range"),
        ("cost $500", r"cost \$500"),
        ("a{b}c", r"a\{b\}c"),
        ("~approx", r"\textasciitilde{}approx"),
    ],
)
def test_latex_special_characters_are_escaped(raw, expected):
    """
    Every one of these appears in real profile text. Unescaped, they are a
    compile failure at best and silently mangled output at worst.
    """
    assert escape(raw) == expected


def test_backslash_escaping_does_not_double_escape():
    """
    The backslash replacement introduces braces that the brace rules would
    then escape again — the sentinel swap is what prevents that.
    """
    assert escape("a\\b") == r"a\textbackslash{}b"


def test_smart_quotes_and_dashes_are_normalized():
    """Pasted profile text is full of these and pdflatex chokes on them."""
    assert "\u2019" not in escape("it\u2019s")
    assert "\u2014" not in escape("a \u2014 b")


def test_urls_keep_their_underscores():
    """Escaping an underscore inside a URL corrupts the address."""
    assert escape_url("https://github.com/a_b/c_d") == "https://github.com/a_b/c_d"
    assert escape_url("https://x.com/a%20b") == r"https://x.com/a\%20b"


def test_link_renders_address_into_the_text_layer():
    """
    Most ATS parsers read the text layer and ignore link annotations, so a
    link rendered only as "GitHub" loses the address entirely.
    """
    out = link("https://github.com/testcandidate")
    assert "github.com/testcandidate" in out
    assert r"\visiblelink" in out


def test_empty_section_is_omitted():
    """A bare heading with nothing under it reads as a generation bug."""
    assert section("Projects", "") == ""
    assert section("Projects", "   ") == ""
    assert "Projects" in section("Projects", "content")


def test_date_range_does_not_invent_missing_dates():
    assert format_date_range(date(2022, 1, 1), None, is_current=True) == "Jan 2022 -- Present"
    assert format_date_range(None, None) == ""
    assert format_date_range(date(2022, 1, 1), date(2023, 6, 1)) == "Jan 2022 -- Jun 2023"


# ============================================================== gap analysis

def test_listed_skill_is_supported(rich_profile):
    verdict = GapAnalyzer(rich_profile).classify("Kubernetes")
    assert verdict.status is KeywordStatus.SUPPORTED_BUT_ABSENT
    assert verdict.evidence
    assert verdict.may_emphasize


def test_skill_alias_is_recognised(rich_profile):
    """"k8s" on a JD means the candidate's "Kubernetes" experience."""
    assert GapAnalyzer(rich_profile).classify("k8s").may_emphasize


def test_project_technology_counts_as_evidence(rich_profile):
    """
    A skill used in a real project is genuinely supported even when it is not
    on the listed-skills roster.
    """
    verdict = GapAnalyzer(rich_profile).classify("Go")
    assert verdict.may_emphasize
    assert "Project" in verdict.evidence


def test_unsupported_skill_is_absent(rich_profile):
    verdict = GapAnalyzer(rich_profile).classify("Salesforce")
    assert verdict.status is KeywordStatus.ABSENT
    assert not verdict.may_emphasize
    assert verdict.evidence == ""


def test_adjacent_technology_is_transferable_not_claimable(rich_profile):
    """
    The candidate has GCP, not AWS. AWS is adjacent — reportable as context,
    but it must never be written onto the resume as if held.
    """
    verdict = GapAnalyzer(rich_profile).classify("AWS")
    assert verdict.status is KeywordStatus.TRANSFERABLE
    assert verdict.via_skill == "GCP"
    assert not verdict.may_emphasize


def test_transferable_groups_stay_tight(rich_profile):
    """
    A loose adjacency graph is how a resume ends up implying experience the
    candidate does not have. Kubernetes must not make Salesforce transferable.
    """
    assert GapAnalyzer(rich_profile).classify("Salesforce").status is KeywordStatus.ABSENT
    assert GapAnalyzer(rich_profile).classify("SAP").status is KeywordStatus.ABSENT


def test_skill_already_on_the_resume_is_under_represented(rich_profile):
    analyzer = GapAnalyzer(rich_profile, resume_text="Experienced with Kubernetes at Acme.")
    assert analyzer.classify("Kubernetes").status is KeywordStatus.UNDER_REPRESENTED


def test_skill_stated_repeatedly_needs_nothing(rich_profile):
    """Re-emphasizing an already-prominent skill is just keyword stuffing."""
    text = "Kubernetes platform work. Kubernetes operators. Kubernetes at scale."
    verdict = GapAnalyzer(rich_profile, resume_text=text).classify("Kubernetes")
    assert verdict.status is KeywordStatus.WELL_REPRESENTED
    assert not verdict.may_emphasize


def test_gap_report_separates_the_four_categories(rich_profile):
    report = GapAnalyzer(rich_profile).analyze(["Kubernetes", "Terraform", "AWS", "Salesforce"])

    assert {v.keyword for v in report.emphasizable} == {"Kubernetes", "Terraform"}
    assert [v.keyword for v in report.transferable] == ["AWS"]
    assert [v.keyword for v in report.absent] == ["Salesforce"]


def test_tailoring_reason_names_what_was_omitted(rich_profile):
    """The user should be able to see what a job wanted that they lack."""
    report = GapAnalyzer(rich_profile).analyze(["Kubernetes", "Terraform", "Salesforce"])
    assert "Salesforce" in report.tailoring_reason()
    assert "not supported" in report.tailoring_reason()


def test_should_not_tailor_when_nothing_to_surface(rich_profile):
    report = GapAnalyzer(rich_profile).analyze(["Salesforce", "SAP"])
    assert report.should_tailor() is False


# ================================================================ generation

def test_generated_resume_contains_real_content(rich_profile):
    result = ResumeGenerator().generate(rich_profile)
    tex = result.tex

    assert rich_profile.full_name in tex
    assert "Acme Corp" in tex
    assert "Cluster Autoscaler" in tex
    assert "Pune University" in tex
    assert "Certified Kubernetes Administrator" in tex


def test_all_real_links_survive_generation(rich_profile):
    """
    Section 30 calls this out specifically, and it fails silently — the
    resume compiles perfectly with the GitHub link gone.
    """
    tex = ResumeGenerator().generate(rich_profile).tex

    for url in (
        "https://github.com/testcandidate",
        "https://linkedin.com/in/testcandidate",
        "https://github.com/testcandidate/autoscaler",
        "https://example.com/autoscaler",
        "https://cert.example.com/cka/123",
    ):
        assert url in tex, f"lost link: {url}"


def test_no_template_placeholders_remain(rich_profile):
    tex = ResumeGenerator().generate(rich_profile).tex
    import re

    assert not re.findall(r"%%[A-Z_]+%%", tex)


def test_tailoring_never_adds_an_unsupported_keyword(rich_profile):
    """
    The central guarantee. A job demanding Salesforce and AWS must produce a
    resume that claims neither.
    """
    report = GapAnalyzer(rich_profile).analyze(["Kubernetes", "AWS", "Salesforce"])
    tex = ResumeGenerator().generate(rich_profile, gap_report=report).tex.lower()

    assert "salesforce" not in tex
    assert "aws" not in tex
    assert "kubernetes" in tex


def test_tailoring_records_what_it_omitted(rich_profile):
    report = GapAnalyzer(rich_profile).analyze(["Kubernetes", "Salesforce"])
    result = ResumeGenerator().generate(rich_profile, gap_report=report)

    assert "Salesforce" in result.keywords_omitted
    assert "Kubernetes" in result.keywords_emphasized
    assert result.supporting_evidence["Kubernetes"]


def test_tailoring_reorders_skills_without_adding_or_removing(rich_profile):
    """
    Reordering is the entire tailoring mechanism for the skills section. The
    set of skills must be identical either way.
    """
    plain = ResumeGenerator().generate(rich_profile).tex
    report = GapAnalyzer(rich_profile).analyze(["Terraform"])
    tailored = ResumeGenerator().generate(rich_profile, gap_report=report).tex

    for skill in rich_profile.skills:
        assert skill.name in plain
        assert skill.name in tailored


def test_experience_stays_reverse_chronological_under_tailoring(rich_profile):
    """
    Reordering roles would misrepresent the career timeline, which is a
    factual claim rather than a presentation choice.
    """
    report = GapAnalyzer(rich_profile).analyze(["Prometheus", "Grafana"])
    tex = ResumeGenerator().generate(rich_profile, gap_report=report).tex

    # Globex is the older role and must still come after Acme, even though
    # the tailoring keywords all point at Globex.
    assert tex.index("Acme Corp") < tex.index("Globex")


def test_bullets_are_reordered_by_relevance(rich_profile):
    report = GapAnalyzer(rich_profile).analyze(["Terraform"])
    tex = ResumeGenerator().generate(rich_profile, gap_report=report).tex

    assert tex.index("Terraform modules") < tex.index("40M requests")


def test_master_resume_reports_no_tailoring(rich_profile):
    result = ResumeGenerator().generate(rich_profile)
    assert result.keywords_emphasized == []
    assert "Master resume" in result.tailoring_reason


def test_generation_survives_a_sparse_profile(session, profile):
    """A profile with only skills must still produce a valid document."""
    tex = ResumeGenerator().generate(profile).tex

    assert r"\begin{document}" in tex
    assert r"\end{document}" in tex
    assert "\\section{Experience}" not in tex  # nothing to show, so omitted


# ================================================================ validation

def test_validator_catches_a_dropped_link(rich_profile):
    tex = ResumeGenerator().generate(rich_profile).tex
    tampered = tex.replace("https://github.com/testcandidate", "")

    report = validate_tex(tampered, rich_profile)
    assert not report.ok
    assert any("github.com/testcandidate" in e for e in report.errors)


def test_validator_catches_a_fabricated_keyword(rich_profile):
    """
    The decisive check: if anything ever did smuggle an unsupported claim
    into the document, validation refuses the resume.
    """
    report = GapAnalyzer(rich_profile).analyze(["Salesforce"])
    tex = ResumeGenerator().generate(rich_profile, gap_report=report).tex
    tampered = tex.replace("\\section{Skills}", "\\section{Skills}\nSalesforce expert.")

    result = validate_tex(tampered, rich_profile, gap_report=report)
    assert not result.ok
    assert any("Salesforce" in e for e in result.errors)


def test_validator_rejects_claiming_a_transferable_keyword(rich_profile):
    """
    The candidate has GCP. Writing "AWS" onto the resume asserts something
    they never said, even though the skill is adjacent.
    """
    report = GapAnalyzer(rich_profile).analyze(["AWS"])
    tex = ResumeGenerator().generate(rich_profile, gap_report=report).tex
    tampered = tex.replace("\\section{Skills}", "\\section{Skills}\nAWS certified.")

    result = validate_tex(tampered, rich_profile, gap_report=report)
    assert not result.ok
    assert any("AWS" in e and "GCP" in e for e in result.errors)


def test_validator_catches_a_missing_section(rich_profile):
    tex = ResumeGenerator().generate(rich_profile).tex
    tampered = tex.replace("\\section{Experience}", "")

    report = validate_tex(tampered, rich_profile)
    assert not report.ok
    assert any("Experience" in e for e in report.errors)


def test_validator_catches_unfilled_placeholders(rich_profile):
    report = validate_tex("\\begin{document}%%SKILLS_SECTION%%\\end{document}", rich_profile)
    assert any("placeholder" in e.lower() for e in report.errors)


def test_validator_passes_a_clean_resume(rich_profile):
    result = ResumeGenerator().generate(rich_profile)
    report = validate_tex(
        result.tex, rich_profile, expected_links=result.links
    )
    assert report.ok, report.errors


def test_validator_does_not_require_trimmed_project_links(session, rich_profile):
    """
    Showing only the most relevant projects is legitimate tailoring. Only the
    links of projects that ARE shown must be present.
    """
    for i in range(8):
        session.add(
            Project(
                profile_id=rich_profile.id, name=f"Extra Project {i}",
                description="Filler", url=f"https://example.com/p{i}",
            )
        )
    session.flush()
    session.refresh(rich_profile)

    result = ResumeGenerator().generate(rich_profile, max_projects=2)
    report = validate_tex(result.tex, rich_profile, expected_links=result.links)
    assert report.ok, report.errors
