"""
Resume gap analysis (section 7) — the truthfulness gate.

Given a job's keywords and the master profile, decide for each keyword which
of four things it is:

1. **SUPPORTED_BUT_ABSENT** — the profile proves it; the resume just does not
   mention it. Safe to surface.
2. **UNDER_REPRESENTED** — the resume mentions it, but buried. Safe to
   promote.
3. **TRANSFERABLE** — the profile has a genuinely adjacent skill. Surfaced
   only as the adjacent skill's real name, never as the keyword itself.
4. **ABSENT** — the profile does not support it at all. Never added, under
   any circumstance.

This module is the single place that decision is made, and it is the reason
the generator can be trusted: the generator cannot add a keyword this module
has not cleared, and every cleared keyword carries the evidence that cleared
it. Section 30's "never fabricate resume content" is enforced structurally
here, not by asking a model to behave.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ..models import Profile
from ..sources.normalize import _collapse


class KeywordStatus(StrEnum):
    SUPPORTED_BUT_ABSENT = "SUPPORTED_BUT_ABSENT"
    UNDER_REPRESENTED = "UNDER_REPRESENTED"
    TRANSFERABLE = "TRANSFERABLE"
    ABSENT = "ABSENT"
    #: Supported and already stated clearly on the current resume. Not one of
    #: section 7's four gap categories because it is not a gap — but the
    #: analyzer has to be able to say so, otherwise a resume that is already
    #: correct looks like one needing work and gets pointlessly regenerated.
    WELL_REPRESENTED = "WELL_REPRESENTED"


#: Genuinely adjacent technologies. Adjacency means "experience with one is
#: real, relevant preparation for the other" — not "both are cloud things".
#: Kept deliberately tight: a loose graph here is how a resume ends up
#: implying experience the candidate does not have.
TRANSFERABLE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"aws", "azure", "gcp"}),                       # major clouds
    frozenset({"jenkins", "gitlab ci", "github actions", "azure devops", "circleci"}),
    frozenset({"terraform", "pulumi", "cloudformation"}),     # IaC
    frozenset({"prometheus", "grafana", "datadog", "splunk", "elk"}),
    frozenset({"postgresql", "mysql"}),                       # relational
    frozenset({"mongodb", "redis"}),                          # non-relational
    frozenset({"pytorch", "tensorflow"}),
    frozenset({"docker", "kubernetes", "openshift"}),
    frozenset({"fastapi", "flask", "django"}),                # python web
    frozenset({"bash", "powershell"}),
)


@dataclass(slots=True)
class KeywordVerdict:
    """One keyword's classification, with the evidence behind it."""

    keyword: str
    status: KeywordStatus
    #: Where in the profile the support comes from. Empty only for ABSENT.
    evidence: str = ""
    #: For TRANSFERABLE: the real skill the candidate actually has. This is
    #: what may appear on the resume — never `keyword` itself.
    via_skill: str = ""

    @property
    def may_emphasize(self) -> bool:
        """Whether the generator is permitted to surface this at all."""
        return self.status in {
            KeywordStatus.SUPPORTED_BUT_ABSENT,
            KeywordStatus.UNDER_REPRESENTED,
        }

    def to_dict(self) -> dict:
        return {
            "keyword": self.keyword,
            "status": str(self.status),
            "evidence": self.evidence,
            "via_skill": self.via_skill,
        }


@dataclass(slots=True)
class GapReport:
    """The full picture for one job."""

    verdicts: list[KeywordVerdict] = field(default_factory=list)

    def by_status(self, status: KeywordStatus) -> list[KeywordVerdict]:
        return [v for v in self.verdicts if v.status is status]

    @property
    def emphasizable(self) -> list[KeywordVerdict]:
        """Keywords the generator is cleared to surface."""
        return [v for v in self.verdicts if v.may_emphasize]

    @property
    def absent(self) -> list[KeywordVerdict]:
        return self.by_status(KeywordStatus.ABSENT)

    @property
    def transferable(self) -> list[KeywordVerdict]:
        return self.by_status(KeywordStatus.TRANSFERABLE)

    def evidence_map(self) -> dict[str, str]:
        """keyword -> evidence, stored on the ResumeVersion for audit."""
        return {v.keyword: v.evidence for v in self.emphasizable}

    def should_tailor(self, *, min_gaps: int = 2) -> bool:
        """
        Whether a job-specific resume is worth generating (section 7).

        Tailoring is only justified when there is something truthful to
        surface. If every relevant keyword is already well represented, the
        master resume is the right document and generating a near-identical
        variant just adds a version to track.
        """
        return len(self.emphasizable) >= min_gaps

    def tailoring_reason(self) -> str:
        """Human-readable justification, stored with the generated resume."""
        if not self.emphasizable:
            return "No truthfully supportable gaps; master resume is appropriate."
        surfaced = [v.keyword for v in self.by_status(KeywordStatus.SUPPORTED_BUT_ABSENT)]
        promoted = [v.keyword for v in self.by_status(KeywordStatus.UNDER_REPRESENTED)]
        parts = []
        if surfaced:
            parts.append(f"surfacing {', '.join(surfaced)} (in profile, absent from resume)")
        if promoted:
            parts.append(f"promoting {', '.join(promoted)} (present but under-represented)")
        if self.absent:
            parts.append(f"omitting {', '.join(v.keyword for v in self.absent)} (not supported by profile)")
        return "Tailored by " + "; ".join(parts) + "."


class GapAnalyzer:
    """
    Classifies job keywords against the master profile.

    `resume_text` is the current resume's text. Without it every supported
    keyword looks absent from the resume, so the UNDER_REPRESENTED case — the
    most common one in practice — could never be detected.
    """

    def __init__(self, profile: Profile, resume_text: str = "") -> None:
        self.profile = profile
        self.resume_text = _collapse(resume_text)
        self._skill_index = self._build_skill_index()
        self._profile_corpus = self._build_corpus()

    def _build_skill_index(self) -> dict[str, tuple[str, str]]:
        """lowercased name or alias -> (canonical name, evidence)."""
        index: dict[str, tuple[str, str]] = {}
        for skill in self.profile.skills:
            evidence = skill.evidence or f"Listed skill ({skill.years or 0:g} yrs)"
            entry = (skill.name, f"Skill '{skill.name}': {evidence}")
            index[skill.name.strip().lower()] = entry
            for alias in skill.aliases or []:
                index[alias.strip().lower()] = entry
        return index

    def _build_corpus(self) -> dict[str, str]:
        """
        Everything the profile asserts, mapped to where it was asserted.

        A keyword can be supported by a project's tech list or an experience
        bullet even when it is not a listed skill — that is real evidence and
        must count.
        """
        corpus: dict[str, str] = {}

        def add(text: str, where: str) -> None:
            for token in _collapse(text).split():
                corpus.setdefault(token, where)
            collapsed = _collapse(text)
            if collapsed:
                corpus.setdefault(collapsed, where)

        for exp in self.profile.experiences:
            where = f"Experience at {exp.company} ({exp.title})"
            for tech in exp.technologies or []:
                add(tech, where)
            for bullet in exp.bullets or []:
                add(bullet, where)

        for proj in self.profile.projects:
            where = f"Project '{proj.name}'"
            for tech in proj.technologies or []:
                add(tech, where)
            for bullet in proj.bullets or []:
                add(bullet, where)
            add(proj.description, where)

        for cert in self.profile.certifications:
            add(cert.name, f"Certification '{cert.name}'")

        return corpus

    # ------------------------------------------------------------ analysis

    def classify(self, keyword: str) -> KeywordVerdict:
        """Classify one keyword. The core truthfulness decision."""
        key = keyword.strip().lower()
        if not key:
            return KeywordVerdict(keyword, KeywordStatus.ABSENT)

        # 1. A listed skill, or an alias of one — strongest evidence.
        if key in self._skill_index:
            _, evidence = self._skill_index[key]
            return self._supported_or_under_represented(keyword, evidence)

        # 2. Asserted somewhere in real work: a project's stack, an
        #    experience bullet, a certification.
        if key in self._profile_corpus:
            return self._supported_or_under_represented(
                keyword, f"Evidenced by: {self._profile_corpus[key]}"
            )

        # 3. Adjacent to something the candidate does have. Reported, but the
        #    keyword itself is never written onto the resume — only the real
        #    skill is, and the reader draws their own conclusion.
        for group in TRANSFERABLE_GROUPS:
            if key in group:
                for sibling in group - {key}:
                    if sibling in self._skill_index:
                        canonical, evidence = self._skill_index[sibling]
                        return KeywordVerdict(
                            keyword,
                            KeywordStatus.TRANSFERABLE,
                            evidence=f"Adjacent experience — {evidence}",
                            via_skill=canonical,
                        )

        # 4. Nothing supports it. It will not appear on the resume.
        return KeywordVerdict(keyword, KeywordStatus.ABSENT)

    def _supported_or_under_represented(self, keyword: str, evidence: str) -> KeywordVerdict:
        """
        Distinguish "not on the resume at all" from "on it, but buried" from
        "already stated clearly".

        The first two are safe to act on and differ in what the generator
        does — add a line versus move one up. The third needs nothing, and
        emphasizing it again would just be keyword stuffing.
        """
        if not self.resume_text:
            return KeywordVerdict(keyword, KeywordStatus.SUPPORTED_BUT_ABSENT, evidence)

        occurrences = self.resume_text.count(_collapse(keyword))
        if occurrences == 0:
            return KeywordVerdict(keyword, KeywordStatus.SUPPORTED_BUT_ABSENT, evidence)
        if occurrences == 1:
            return KeywordVerdict(keyword, KeywordStatus.UNDER_REPRESENTED, evidence)
        return KeywordVerdict(keyword, KeywordStatus.WELL_REPRESENTED, evidence)

    def analyze(self, keywords: Sequence[str]) -> GapReport:
        """Classify every keyword a job asks for."""
        seen: set[str] = set()
        report = GapReport()
        for keyword in keywords:
            key = keyword.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            report.verdicts.append(self.classify(keyword))
        return report
