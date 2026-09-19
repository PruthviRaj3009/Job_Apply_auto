"""
Matching engine (section 6).

Scores a job against the master profile across six dimensions and produces a
single 0..1 figure plus the reasoning behind it.

Three deliberate choices:

**Deterministic first.** Rules and keyword matching produce the score.
Embeddings and the LLM refine it (`semantic.py`, `llm.py`) but cannot
manufacture a match on their own — a hallucinated skill overlap would put the
candidate in front of a job they cannot do.

**Unknown is not a miss.** A JD that does not state a degree requirement has
not failed the education check. Treating silence as failure would reject most
real postings, which routinely omit half these fields.

**Mandatory misses dominate.** Lacking a hard requirement matters far more
than lacking a nice-to-have, so the two are tracked separately and weighted
differently.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..analysis.jd_parser import ParsedJD, extract_skills, parse_jd
from ..models import Job, Profile
from ..models.enums import WorkMode
from ..sources.normalize import _collapse, normalize_location, parse_experience
from .result import DimensionScore, DimensionVerdict, MatchResult

logger = logging.getLogger(__name__)

#: Dimension weights. Skills and role carry the most because they decide
#: whether the candidate can do the job at all; location and preference are
#: filters the user already applied once during discovery.
WEIGHTS: dict[str, float] = {
    "skills": 0.35,
    "role": 0.25,
    "experience": 0.20,
    "location": 0.10,
    "education": 0.05,
    "preference": 0.05,
}

#: Missing a hard requirement costs this much of the final score, per skill,
#: up to `MAX_MANDATORY_PENALTY`.
MANDATORY_MISS_PENALTY = 0.12
MAX_MANDATORY_PENALTY = 0.40


@dataclass(slots=True)
class ProfileSnapshot:
    """
    The profile flattened into what matching needs.

    Built once per run rather than per job: a 200-job batch would otherwise
    walk the same relationships 200 times.
    """

    skills: dict[str, float]  # canonical/lowercased name -> years
    skill_aliases: dict[str, str]  # alias -> canonical
    target_roles: list[str]
    preferred_locations: list[str]
    excluded_keywords: list[str]
    total_experience_years: float
    degrees: list[str]
    certifications: list[str]
    remote_ok: bool = True

    @classmethod
    def from_profile(cls, profile: Profile) -> "ProfileSnapshot":
        skills: dict[str, float] = {}
        aliases: dict[str, str] = {}
        for skill in profile.skills:
            key = skill.name.strip().lower()
            skills[key] = skill.years or 0.0
            aliases[key] = skill.name
            for alias in skill.aliases or []:
                aliases[alias.strip().lower()] = skill.name

        degrees = [
            f"{e.degree} {e.field_of_study}".strip().lower()
            for e in profile.educations
        ]
        prefs = profile.preferences or {}
        return cls(
            skills=skills,
            skill_aliases=aliases,
            target_roles=[r.lower() for r in (profile.target_roles or [])],
            preferred_locations=[normalize_location(loc) for loc in (profile.preferred_locations or [])],
            excluded_keywords=[k.lower() for k in (profile.excluded_keywords or [])],
            total_experience_years=profile.total_experience_years or 0.0,
            degrees=degrees,
            certifications=[c.name.lower() for c in profile.certifications],
            remote_ok=bool(prefs.get("remote_ok", True)),
        )

    def has_skill(self, name: str) -> bool:
        key = name.strip().lower()
        return key in self.skills or key in self.skill_aliases


class MatchingEngine:
    """
    Scores jobs against one profile.

    `semantic` and `llm` are optional collaborators. When absent — Ollama not
    running, no model pulled — matching still works on rules alone and says so
    in `signals_used`, rather than failing or silently scoring everything zero.
    """

    def __init__(
        self,
        profile: Profile | ProfileSnapshot,
        *,
        semantic: object | None = None,
        llm: object | None = None,
    ) -> None:
        self.snapshot = (
            profile if isinstance(profile, ProfileSnapshot) else ProfileSnapshot.from_profile(profile)
        )
        self.semantic = semantic
        self.llm = llm

    # ------------------------------------------------------------- entry

    def match(self, job: Job, parsed: ParsedJD | None = None) -> MatchResult:
        """Compare one job against the profile."""
        result = MatchResult()
        signals = ["rules"]

        if parsed is None:
            parsed = parse_jd(job.description or "")

        jd_skills = self._job_skills(job, parsed)

        result.skills_match, result.matching_skills, result.missing_skills = self._score_skills(jd_skills)
        result.missing_mandatory_skills = self._mandatory_misses(parsed, result.missing_skills)
        result.role_match = self._score_role(job)
        result.experience_match = self._score_experience(job, parsed)
        result.location_match = self._score_location(job)
        result.education_match = self._score_education(parsed)
        result.preference_match = self._score_preferences(job)

        base = sum(
            WEIGHTS[name] * getattr(result, f"{name}_match").score
            for name in WEIGHTS
        )

        penalty = min(
            MAX_MANDATORY_PENALTY,
            MANDATORY_MISS_PENALTY * len(result.missing_mandatory_skills),
        )
        score = max(0.0, base - penalty)

        # --- optional refinements -------------------------------------
        if self.semantic is not None:
            try:
                similarity = self.semantic.similarity(job)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - never let RAG break matching
                logger.debug("Semantic scoring unavailable: %s", exc)
            else:
                if similarity is not None:
                    # Blend rather than replace: the deterministic score stays
                    # the anchor, so a confidently wrong embedding cannot on
                    # its own promote an unsuitable job.
                    score = 0.75 * score + 0.25 * similarity
                    signals.append("embeddings")

        result.match_score = round(min(1.0, max(0.0, score)), 3)
        result.concerns = self._collect_concerns(job, result)
        result.signals_used = signals
        result.match_explanation = self._explain(job, result)
        return result

    # ------------------------------------------------------- dimensions

    def _job_skills(self, job: Job, parsed: ParsedJD) -> list[str]:
        """
        Skills the job wants: whatever the source supplied, plus whatever the
        JD text yields. Sources list tags the prose omits and vice versa.
        """
        found = list(parsed.skills)
        for raw_skill in job.skills or []:
            for canonical in extract_skills(raw_skill) or [raw_skill.strip()]:
                if canonical and canonical not in found:
                    found.append(canonical)
        return found

    def _score_skills(self, jd_skills: Sequence[str]) -> tuple[DimensionScore, list[str], list[str]]:
        if not jd_skills:
            # No extractable skills is a parsing gap, not a mismatch. Scoring
            # it zero would sink every short or unstructured posting.
            return (
                DimensionScore(DimensionVerdict.UNKNOWN, 0.5, "No skills could be extracted from the JD"),
                [],
                [],
            )

        matching = [s for s in jd_skills if self.snapshot.has_skill(s)]
        missing = [s for s in jd_skills if not self.snapshot.has_skill(s)]
        ratio = len(matching) / len(jd_skills)

        if ratio >= 0.75:
            verdict = DimensionVerdict.MATCH
        elif ratio >= 0.4:
            verdict = DimensionVerdict.PARTIAL
        else:
            verdict = DimensionVerdict.MISMATCH

        reason = f"{len(matching)}/{len(jd_skills)} required skills present"
        return DimensionScore(verdict, ratio, reason), matching, missing

    def _mandatory_misses(self, parsed: ParsedJD, missing: Sequence[str]) -> list[str]:
        """
        Which missing skills the JD called hard requirements.

        A skill is mandatory only if it appears in a line the parser marked
        mandatory — inferring it from position or emphasis produces false
        rejections.
        """
        if not parsed.mandatory_requirements or not missing:
            return []
        blob = " ".join(parsed.mandatory_requirements).lower()
        return [skill for skill in missing if skill.lower() in blob]

    def _score_role(self, job: Job) -> DimensionScore:
        if not self.snapshot.target_roles:
            return DimensionScore(DimensionVerdict.UNKNOWN, 0.5, "No target roles set")

        title = _collapse(job.title)
        best = 0.0
        best_role = ""
        for role in self.snapshot.target_roles:
            role_norm = _collapse(role)
            if not role_norm:
                continue
            if role_norm in title:
                best, best_role = 1.0, role
                break
            overlap = _token_overlap(set(role_norm.split()), set(title.split()))
            if overlap > best:
                best, best_role = overlap, role

        if best >= 0.9:
            return DimensionScore(DimensionVerdict.MATCH, 1.0, f"Title matches target role '{best_role}'")
        if best >= 0.5:
            return DimensionScore(DimensionVerdict.PARTIAL, best, f"Title partially matches '{best_role}'")
        return DimensionScore(DimensionVerdict.MISMATCH, best, f"'{job.title}' is not among your target roles")

    def _score_experience(self, job: Job, parsed: ParsedJD) -> DimensionScore:
        required = job.experience_min_years
        if required is None:
            required, _ = parse_experience(" ".join(parsed.mandatory_requirements) or job.description or "")
        if required is None:
            return DimensionScore(DimensionVerdict.UNKNOWN, 0.6, "JD does not state an experience requirement")

        have = self.snapshot.total_experience_years
        if have >= required:
            return DimensionScore(DimensionVerdict.MATCH, 1.0, f"{have:g} yrs meets the {required:g} yr requirement")

        gap = required - have
        if gap <= 1:
            return DimensionScore(
                DimensionVerdict.PARTIAL, 0.7,
                f"{have:g} yrs against {required:g} required — within normal hiring latitude",
            )
        if gap <= 2:
            return DimensionScore(DimensionVerdict.PARTIAL, 0.4, f"{gap:g} yrs short of the requirement")
        return DimensionScore(DimensionVerdict.MISMATCH, 0.0, f"{gap:g} yrs short of the {required:g} yr requirement")

    def _score_location(self, job: Job) -> DimensionScore:
        if job.work_mode is WorkMode.REMOTE:
            if self.snapshot.remote_ok:
                return DimensionScore(DimensionVerdict.MATCH, 1.0, "Remote role")
            return DimensionScore(DimensionVerdict.MISMATCH, 0.0, "Remote, but you prefer on-site")

        job_loc = normalize_location(job.location)
        if not job_loc:
            return DimensionScore(DimensionVerdict.UNKNOWN, 0.5, "No location stated")

        if not self.snapshot.preferred_locations:
            return DimensionScore(DimensionVerdict.UNKNOWN, 0.5, "No preferred locations set")

        if job_loc in self.snapshot.preferred_locations:
            return DimensionScore(DimensionVerdict.MATCH, 1.0, f"{job.location} is a preferred location")
        return DimensionScore(
            DimensionVerdict.MISMATCH, 0.0,
            f"{job.location} is not among your preferred locations",
        )

    def _score_education(self, parsed: ParsedJD) -> DimensionScore:
        requirement_text = " ".join(parsed.mandatory_requirements + parsed.optional_requirements).lower()
        if not requirement_text:
            return DimensionScore(DimensionVerdict.UNKNOWN, 0.6, "JD states no education requirement")

        wanted = [
            level
            for level, pattern in (
                ("phd", r"\bph\.?d\b|\bdoctorate\b"),
                ("masters", r"\bmasters?\b|\bm\.?s\.?\b|\bm\.?tech\b|\bmba\b"),
                ("bachelors", r"\bbachelors?\b|\bb\.?s\.?\b|\bb\.?tech\b|\bb\.?e\.?\b|\bdegree\b"),
            )
            if re.search(pattern, requirement_text)
        ]
        if not wanted:
            return DimensionScore(DimensionVerdict.UNKNOWN, 0.6, "JD states no education requirement")

        held = " ".join(self.snapshot.degrees)
        levels_held = {
            "phd": bool(re.search(r"ph\.?d|doctorate", held)),
            "masters": bool(re.search(r"master|m\.?s|m\.?tech|mba", held)),
            "bachelors": bool(re.search(r"bachelor|b\.?s|b\.?tech|b\.?e\b|engineering", held)),
        }
        # A master's satisfies a bachelor's requirement.
        if levels_held["phd"]:
            levels_held["masters"] = levels_held["bachelors"] = True
        elif levels_held["masters"]:
            levels_held["bachelors"] = True

        if any(levels_held.get(level) for level in wanted):
            return DimensionScore(DimensionVerdict.MATCH, 1.0, f"Education meets the {wanted[-1]} requirement")
        return DimensionScore(DimensionVerdict.MISMATCH, 0.0, f"JD requires a {wanted[-1]} degree")

    def _score_preferences(self, job: Job) -> DimensionScore:
        blob = _collapse(f"{job.title} {job.description or ''}")
        hits = [kw for kw in self.snapshot.excluded_keywords if kw and _collapse(kw) in blob]
        if hits:
            return DimensionScore(
                DimensionVerdict.MISMATCH, 0.0,
                f"Mentions excluded terms: {', '.join(hits[:3])}",
            )
        return DimensionScore(DimensionVerdict.MATCH, 1.0, "No excluded terms present")

    # ------------------------------------------------------- narrative

    def _collect_concerns(self, job: Job, result: MatchResult) -> list[str]:
        """
        Things worth telling the user before they apply.

        Distinct from rejection reasons: a concern does not block an
        application, it is context for the decision.
        """
        concerns: list[str] = []
        if result.missing_mandatory_skills:
            concerns.append(
                "Missing skills the JD lists as required: "
                + ", ".join(result.missing_mandatory_skills)
            )
        if result.experience_match.verdict is DimensionVerdict.PARTIAL:
            concerns.append(result.experience_match.reason)
        if result.location_match.verdict is DimensionVerdict.MISMATCH:
            concerns.append(result.location_match.reason)
        if job.salary_min is None and job.salary_text:
            concerns.append(f"Salary not machine-readable: {job.salary_text!r}")
        if not job.description:
            concerns.append("No job description was available — match is based on the title alone")
        if not job.easy_apply:
            concerns.append("Not an easy-apply posting; the application may need manual steps")
        return concerns

    def _explain(self, job: Job, result: MatchResult) -> str:
        """One readable paragraph, for the dashboard and the sheet."""
        verdict = (
            "Strong match" if result.match_score >= 0.8
            else "Reasonable match" if result.match_score >= 0.6
            else "Weak match" if result.match_score >= 0.4
            else "Poor match"
        )
        parts = [f"{verdict} ({result.match_score:.0%}) for {job.title} at {job.company}."]

        if result.matching_skills:
            parts.append("Overlapping skills: " + ", ".join(result.matching_skills[:8]) + ".")
        if result.missing_skills:
            parts.append("Not evidenced in your profile: " + ", ".join(result.missing_skills[:8]) + ".")
        parts.append(result.role_match.reason + ".")
        parts.append(result.experience_match.reason + ".")
        if result.location_match.verdict is not DimensionVerdict.UNKNOWN:
            parts.append(result.location_match.reason + ".")
        return " ".join(parts)


def _token_overlap(a: set[str], b: set[str]) -> float:
    """Share of `a`'s tokens present in `b`."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)
