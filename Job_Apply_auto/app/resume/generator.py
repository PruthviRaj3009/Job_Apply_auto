"""
Profile -> LaTeX generation (section 8).

The generator is deterministic. It selects and orders content the user
actually wrote; it never composes new claims. Where a job's keywords matter,
they influence **ordering and inclusion** — which skills lead, which projects
appear first — and nothing else. An LLM is not in this path at all, which is
what makes section 30's "do not let the LLM freely invent resume content"
structurally true rather than a hope.

The only content that may be surfaced for a job is what `GapAnalyzer` has
cleared, and each cleared keyword is stored with the evidence that cleared it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Profile
from ..sources.normalize import _collapse
from .gap_analysis import GapReport, KeywordStatus
from .latex import escape, format_date_range, itemize, link, section

logger = logging.getLogger(__name__)

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "resume_templates"
DEFAULT_TEMPLATE = TEMPLATE_ROOT / "base_resume.tex"


@dataclass(slots=True)
class GeneratedResume:
    """A rendered resume plus the record of how it was tailored."""

    tex: str
    keywords_emphasized: list[str] = field(default_factory=list)
    keywords_omitted: list[str] = field(default_factory=list)
    supporting_evidence: dict[str, str] = field(default_factory=dict)
    tailoring_reason: str = ""
    #: Every real link that must survive into the PDF, for the validator.
    links: list[str] = field(default_factory=list)


class ResumeGenerator:
    """
    Renders a profile into LaTeX, optionally ordered for a specific job.

    `gap_report` is the authority on what may be emphasized. Passing None
    produces the master resume: everything in natural order, nothing tailored.
    """

    def __init__(self, template_path: Path | None = None) -> None:
        self.template_path = template_path or DEFAULT_TEMPLATE

    def generate(
        self,
        profile: Profile,
        *,
        gap_report: GapReport | None = None,
        max_bullets_per_role: int = 4,
        max_projects: int = 4,
    ) -> GeneratedResume:
        template = self.template_path.read_text(encoding="utf-8")

        priority = self._priority_keywords(gap_report)
        links: list[str] = []

        replacements = {
            "FULL_NAME": escape(profile.full_name),
            "CONTACT_LINE": self._contact_line(profile, links),
            "SUMMARY_SECTION": self._summary(profile),
            "SKILLS_SECTION": self._skills(profile, priority),
            "EXPERIENCE_SECTION": self._experience(profile, priority, max_bullets_per_role),
            "PROJECTS_SECTION": self._projects(profile, priority, max_projects, links),
            "EDUCATION_SECTION": self._education(profile),
            "CERTIFICATIONS_SECTION": self._certifications(profile, links),
            "ACHIEVEMENTS_SECTION": self._achievements(profile),
            "PUBLICATIONS_SECTION": self._publications(profile),
        }

        tex = template
        for token, value in replacements.items():
            tex = tex.replace(f"%%{token}%%", value)

        result = GeneratedResume(tex=tex, links=links)
        if gap_report is not None:
            result.keywords_emphasized = [v.keyword for v in gap_report.emphasizable]
            result.keywords_omitted = [v.keyword for v in gap_report.absent]
            result.supporting_evidence = gap_report.evidence_map()
            result.tailoring_reason = gap_report.tailoring_reason()
        else:
            result.tailoring_reason = "Master resume — no job-specific tailoring."
        return result

    # --------------------------------------------------------- ordering

    @staticmethod
    def _priority_keywords(gap_report: GapReport | None) -> set[str]:
        """
        Lowercased keywords that may influence ordering.

        Only cleared keywords are included. ABSENT ones are excluded here as
        well as from the content, so a job's unmet requirement cannot even
        change what gets shown — which would misrepresent emphasis.
        """
        if gap_report is None:
            return set()
        return {v.keyword.lower() for v in gap_report.emphasizable}

    @staticmethod
    def _relevance(text: str, priority: set[str]) -> int:
        """How many priority keywords a piece of content mentions."""
        if not priority:
            return 0
        blob = _collapse(text)
        return sum(1 for kw in priority if _collapse(kw) in blob)

    # --------------------------------------------------------- sections

    def _contact_line(self, profile: Profile, links: list[str]) -> str:
        parts: list[str] = []
        if profile.email:
            parts.append(link(f"mailto:{profile.email}", profile.email))
        if profile.phone:
            parts.append(escape(profile.phone))
        if profile.location:
            parts.append(escape(profile.location))
        for url in (profile.linkedin_url, profile.github_url, profile.portfolio_url):
            if url:
                parts.append(link(url))
                links.append(url)
        for url in (profile.other_links or {}).values():
            if url:
                parts.append(link(url))
                links.append(url)
        return r" $\vert$ ".join(parts)

    def _summary(self, profile: Profile) -> str:
        if not profile.summary and not profile.headline:
            return ""
        body = escape(profile.summary or profile.headline)
        return section("Summary", body)

    def _skills(self, profile: Profile, priority: set[str]) -> str:
        """
        Skills grouped by category, priority skills first within each group.

        Reordering is the entire tailoring mechanism for this section. No
        skill is added, removed or renamed — a recruiter scanning the top of
        the list sees what this job asks for, and everything else is still
        there.
        """
        if not profile.skills:
            return ""

        groups: dict[str, list] = {}
        for skill in profile.skills:
            groups.setdefault(skill.category or "Technical", []).append(skill)

        lines: list[str] = []
        for category in sorted(groups):
            ordered = sorted(
                groups[category],
                key=lambda s: (
                    0 if s.name.lower() in priority else 1,
                    -(s.years or 0),
                    s.name.lower(),
                ),
            )
            names = ", ".join(escape(s.name) for s in ordered)
            lines.append(rf"\labelled{{{escape(category)}}}{{{names}}}")
        return section("Skills", "\n".join(lines))

    def _experience(self, profile: Profile, priority: set[str], max_bullets: int) -> str:
        """
        Experience in reverse-chronological order — always.

        Only the *bullets within* a role are reordered by relevance. Reordering
        the roles themselves would misrepresent the career timeline, which is
        a factual claim, not a presentation choice.
        """
        roles = [e for e in profile.experiences if not e.is_internship]
        internships = [e for e in profile.experiences if e.is_internship]
        if not roles and not internships:
            return ""

        def render(entries: list) -> str:
            out: list[str] = []
            for exp in sorted(
                entries,
                key=lambda e: (e.is_current, e.start_date or _MIN_DATE),
                reverse=True,
            ):
                dates = format_date_range(exp.start_date, exp.end_date, exp.is_current)
                out.append(
                    rf"\entry{{{escape(exp.company)}}}{{{escape(exp.location)}}}"
                    rf"{{{escape(exp.title)}}}{{{escape(dates)}}}"
                )
                bullets = sorted(
                    exp.bullets or [],
                    key=lambda b: -self._relevance(b, priority),
                )
                out.append(itemize(bullets[:max_bullets]))
                # The role's stack is real profile data and was being dropped
                # entirely. It is also what an ATS keyword-matches against, so
                # omitting it costs the candidate matches on skills they have.
                if exp.technologies:
                    tech = ", ".join(escape(t) for t in exp.technologies)
                    out.append(rf"\textit{{Technologies:}} {tech}\\[0.3em]")
            return "\n".join(p for p in out if p)

        body = section("Experience", render(roles))
        if internships:
            body += section("Internships", render(internships))
        return body

    def _projects(
        self, profile: Profile, priority: set[str], max_projects: int, links: list[str]
    ) -> str:
        """
        Projects, most relevant first, each carrying its real URLs.

        Project and repo links are collected for the validator: losing them is
        a specific failure section 30 calls out, and it happens silently.
        """
        if not profile.projects:
            return ""

        ranked = sorted(
            profile.projects,
            key=lambda p: (
                -self._relevance(
                    f"{p.name} {p.description} {' '.join(p.technologies or [])} {' '.join(p.bullets or [])}",
                    priority,
                ),
                p.sort_order,
            ),
        )

        out: list[str] = []
        for project in ranked[:max_projects]:
            url_parts = []
            for url in (project.url, project.repo_url):
                if url:
                    url_parts.append(link(url))
                    links.append(url)
            right = r" $\vert$ ".join(url_parts)

            tech = ", ".join(escape(t) for t in (project.technologies or []))
            out.append(
                rf"\entry{{{escape(project.name)}}}{{{right}}}{{{tech}}}{{}}"
            )
            bullets = list(project.bullets or [])
            if not bullets and project.description:
                bullets = [project.description]
            out.append(itemize(bullets))
        return section("Projects", "\n".join(p for p in out if p))

    def _education(self, profile: Profile) -> str:
        if not profile.educations:
            return ""
        out: list[str] = []
        for edu in sorted(
            profile.educations, key=lambda e: e.end_date or _MIN_DATE, reverse=True
        ):
            dates = format_date_range(edu.start_date, edu.end_date)
            degree = " ".join(p for p in (edu.degree, edu.field_of_study) if p)
            out.append(
                rf"\entry{{{escape(edu.institution)}}}{{{escape(edu.grade)}}}"
                rf"{{{escape(degree)}}}{{{escape(dates)}}}"
            )
        return section("Education", "\n".join(out))

    def _certifications(self, profile: Profile, links: list[str]) -> str:
        if not profile.certifications:
            return ""
        items: list[str] = []
        for cert in profile.certifications:
            text = escape(cert.name)
            if cert.issuer:
                text += f" -- {escape(cert.issuer)}"
            if cert.issued_date:
                text += f" ({cert.issued_date.strftime('%b %Y')})"
            if cert.credential_url:
                text += " " + link(cert.credential_url, "verify")
                links.append(cert.credential_url)
            items.append(text)
        return section("Certifications", itemize(items, escape_items=False))

    def _achievements(self, profile: Profile) -> str:
        items = (profile.preferences or {}).get("achievements") or []
        return section("Achievements", itemize(list(items)))

    def _publications(self, profile: Profile) -> str:
        items = (profile.preferences or {}).get("publications") or []
        return section("Publications", itemize(list(items)))


class _MinDate:
    """
    Sort sentinel for undated entries.

    Comparing against `date.min` directly fails when some dates are None, and
    defaulting a missing date to today would reorder a career.
    """

    def __lt__(self, other: object) -> bool:
        return True

    def __gt__(self, other: object) -> bool:
        return False

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _MinDate)

    def __le__(self, other: object) -> bool:
        return True

    def __ge__(self, other: object) -> bool:
        return isinstance(other, _MinDate)

    def __hash__(self) -> int:
        return hash("_MinDate")


_MIN_DATE = _MinDate()
