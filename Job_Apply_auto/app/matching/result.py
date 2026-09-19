"""
The shape of a match decision (section 6).

Every field the spec asks for is here, and the result is serializable so it
lands whole in `Job.match_detail` — a score with no reasoning behind it is
useless when the user asks why a job was rejected.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class DimensionVerdict(StrEnum):
    """
    Per-dimension outcome.

    `UNKNOWN` is a real answer, not a failure: a JD that does not state a
    degree requirement has not failed the education check, and scoring it as a
    miss would penalise the many postings that simply say nothing.
    """

    MATCH = "MATCH"
    PARTIAL = "PARTIAL"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class DimensionScore:
    """One scored axis, with the reason it scored that way."""

    verdict: DimensionVerdict
    score: float
    reason: str = ""

    def to_dict(self) -> dict:
        return {"verdict": str(self.verdict), "score": round(self.score, 3), "reason": self.reason}


@dataclass(slots=True)
class MatchResult:
    """The full comparison of one job against the master profile."""

    match_score: float = 0.0
    matching_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    #: Missing skills the JD marked as hard requirements — these drive
    #: rejection far more than a missing nice-to-have.
    missing_mandatory_skills: list[str] = field(default_factory=list)

    experience_match: DimensionScore = field(
        default_factory=lambda: DimensionScore(DimensionVerdict.UNKNOWN, 0.0)
    )
    education_match: DimensionScore = field(
        default_factory=lambda: DimensionScore(DimensionVerdict.UNKNOWN, 0.0)
    )
    location_match: DimensionScore = field(
        default_factory=lambda: DimensionScore(DimensionVerdict.UNKNOWN, 0.0)
    )
    role_match: DimensionScore = field(
        default_factory=lambda: DimensionScore(DimensionVerdict.UNKNOWN, 0.0)
    )
    preference_match: DimensionScore = field(
        default_factory=lambda: DimensionScore(DimensionVerdict.UNKNOWN, 0.0)
    )
    skills_match: DimensionScore = field(
        default_factory=lambda: DimensionScore(DimensionVerdict.UNKNOWN, 0.0)
    )

    #: Things the user should know before applying — a long commute, a
    #: seniority gap, an excluded technology. Not automatic rejections.
    concerns: list[str] = field(default_factory=list)
    match_explanation: str = ""
    #: Which scorers actually contributed, so a run without Ollama is
    #: distinguishable from one where semantic scoring found nothing.
    signals_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in (
            "experience_match", "education_match", "location_match",
            "role_match", "preference_match", "skills_match",
        ):
            data[key] = getattr(self, key).to_dict()
        data["match_score"] = round(self.match_score, 3)
        return data
