"""
Sensitive-question detection (section 12).

A sensitive question always goes back to the user for explicit confirmation,
**even when a confident stored answer exists**. That is the whole point: these
are legal declarations, protected-characteristic disclosures and binding
commitments. Getting one wrong is not a bad match, it is a false statement on
a legal document or an unintended disclosure.

The detector is deliberately over-inclusive. A false positive costs the user
one confirmation click. A false negative auto-answers a work-authorization
question on their behalf.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models.enums import QuestionCategory


@dataclass(frozen=True, slots=True)
class SensitiveTopic:
    key: str
    category: QuestionCategory
    #: Why this is sensitive, shown to the user when we ask.
    rationale: str
    patterns: tuple[str, ...]


#: Ordered most-specific first, so a question matching several topics is
#: reported under the most precise one.
SENSITIVE_TOPICS: tuple[SensitiveTopic, ...] = (
    SensitiveTopic(
        "work_authorization",
        QuestionCategory.SENSITIVE,
        "Work authorization is a legal declaration — an incorrect answer can "
        "invalidate an application or an offer.",
        (
            r"legally\s+(?:authorized|authorised|entitled|eligible)\s+to\s+work",
            r"(?:authorized|authorised)\s+to\s+work",
            r"right\s+to\s+work",
            r"work\s+(?:permit|authorization|authorisation|eligibility)",
            r"eligible\s+to\s+work",
            r"employment\s+eligibility",
            r"\bi-?9\b",
            r"\bwork\s+visa\b",
        ),
    ),
    SensitiveTopic(
        "visa_sponsorship",
        QuestionCategory.SENSITIVE,
        "Sponsorship answers are binding and frequently screen candidates out; "
        "only you can state your position.",
        (
            r"(?:require|need|seek|request).{0,25}sponsor",
            r"sponsorship",
            r"\bh-?1b?\b", r"\bopt\b", r"\bcpt\b", r"\btn\s+visa\b",
            r"visa\s+status",
            r"now\s+or\s+in\s+the\s+future.{0,40}(?:sponsor|visa)",
        ),
    ),
    SensitiveTopic(
        "disability",
        QuestionCategory.SENSITIVE,
        "Disability status is a protected characteristic and voluntary to disclose.",
        (
            r"\bdisabilit(?:y|ies)\b",
            r"\bdisabled\b",
            r"reasonable\s+accommodation",
            r"\bimpairment\b",
            r"chronic\s+(?:illness|condition)",
        ),
    ),
    SensitiveTopic(
        "veteran_status",
        QuestionCategory.SENSITIVE,
        "Veteran status is a protected characteristic and voluntary to disclose.",
        (
            r"\bveteran\b", r"\bmilitary\s+service\b",
            r"armed\s+forces", r"protected\s+veteran",
        ),
    ),
    SensitiveTopic(
        "race_ethnicity",
        QuestionCategory.SENSITIVE,
        "Race and ethnicity are protected characteristics and voluntary to disclose.",
        (
            r"\brace\b", r"\bethnicit(?:y|ies)\b", r"\bethnic\s+(?:group|origin)\b",
            r"racial", r"\bhispanic\b", r"\blatino\b",
            r"\bcaste\b", r"\bnationalit(?:y|ies)\b",
        ),
    ),
    SensitiveTopic(
        "gender_identity",
        QuestionCategory.SENSITIVE,
        "Gender and sexual orientation are protected characteristics and "
        "voluntary to disclose.",
        (
            r"\bgender\b", r"\bsex\b(?!\s*(?:ual\s+harassment))",
            r"sexual\s+orientation", r"\blgbt", r"\bpronouns?\b",
            r"self[-\s]?identif",
            r"\btransgender\b",
        ),
    ),
    SensitiveTopic(
        "criminal_history",
        QuestionCategory.SENSITIVE,
        "Criminal-history questions are legal declarations with serious "
        "consequences for a wrong answer.",
        (
            r"\bconvict(?:ed|ion)\b", r"criminal\s+(?:record|history|background)",
            r"\bfelony\b", r"\bmisdemean(?:o|ou)r\b",
            r"\barrested\b", r"pleaded\s+guilty",
            r"background\s+(?:check|screening|verification)",
        ),
    ),
    SensitiveTopic(
        "salary_expectations",
        QuestionCategory.COMPENSATION,
        "A stated salary figure anchors the whole negotiation.",
        (
            r"salary\s+(?:expectation|requirement|desired)",
            r"expected\s+(?:ctc|salary|compensation|package)",
            r"current\s+(?:ctc|salary|compensation|package)",
            r"desired\s+(?:salary|compensation|pay)",
            r"compensation\s+expectation",
            r"what.{0,20}(?:salary|ctc).{0,20}(?:expect|looking)",
        ),
    ),
    SensitiveTopic(
        "relocation_commitment",
        QuestionCategory.LOGISTICS,
        "Relocation is a personal commitment only you can make.",
        (
            r"willing\s+to\s+relocat", r"able\s+to\s+relocat",
            r"open\s+to\s+relocat", r"\brelocation\b",
            r"willing\s+to\s+(?:move|travel)",
            r"comfortable\s+(?:working|commuting)\s+(?:from|to)",
        ),
    ),
    SensitiveTopic(
        "legal_declaration",
        QuestionCategory.SENSITIVE,
        "This is a formal declaration being made in your name.",
        (
            r"\bcertify\b", r"\bdeclare\b", r"\battest\b",
            r"under\s+penalty\s+of\s+perjury",
            r"\bi\s+confirm\s+that\b",
            r"terms\s+and\s+conditions",
            r"privacy\s+(?:policy|notice)",
            r"\bconsent\s+to\b", r"\bauthorize\s+(?:the\s+)?(?:company|employer)",
            r"non[-\s]?compete", r"\bnda\b",
            r"\bdrug\s+(?:test|screen)",
        ),
    ),
    SensitiveTopic(
        "age_dob",
        QuestionCategory.SENSITIVE,
        "Age and date of birth are protected characteristics in most jurisdictions.",
        (
            r"date\s+of\s+birth", r"\bdob\b",
            r"\bhow\s+old\b", r"\byour\s+age\b",
            r"\bage\b(?!\s*(?:nt|ncy|nda))",
            r"year\s+of\s+birth",
        ),
    ),
    SensitiveTopic(
        "marital_family",
        QuestionCategory.SENSITIVE,
        "Marital and family status are protected characteristics.",
        (
            r"marital\s+status", r"\bmarried\b",
            r"\bdependents?\b", r"\bchildren\b",
            r"\bpregnan", r"\bmaternity\b",
        ),
    ),
    SensitiveTopic(
        "religion",
        QuestionCategory.SENSITIVE,
        "Religion is a protected characteristic and voluntary to disclose.",
        (r"\breligio(?:n|us)\b", r"\bfaith\b", r"\bcreed\b"),
    ),
)

_COMPILED: tuple[tuple[SensitiveTopic, tuple[re.Pattern[str], ...]], ...] = tuple(
    (topic, tuple(re.compile(p, re.IGNORECASE) for p in topic.patterns))
    for topic in SENSITIVE_TOPICS
)


@dataclass(slots=True)
class SensitivityVerdict:
    is_sensitive: bool
    topic: str = ""
    category: QuestionCategory = QuestionCategory.OTHER
    rationale: str = ""

    def __bool__(self) -> bool:
        return self.is_sensitive


NOT_SENSITIVE = SensitivityVerdict(False)


def classify_sensitivity(question: str) -> SensitivityVerdict:
    """
    Decide whether a question needs explicit user confirmation.

    Over-inclusive on purpose: a false positive costs one click, a false
    negative auto-answers a legal declaration on the user's behalf.
    """
    if not question or not question.strip():
        return NOT_SENSITIVE

    for topic, patterns in _COMPILED:
        if any(p.search(question) for p in patterns):
            return SensitivityVerdict(True, topic.key, topic.category, topic.rationale)
    return NOT_SENSITIVE


def is_sensitive(question: str) -> bool:
    return classify_sensitivity(question).is_sensitive


#: Topic -> category, for questions that are sensitive but also belong to a
#: normal category (salary is COMPENSATION, relocation is LOGISTICS).
def categorize(question: str) -> QuestionCategory:
    """
    Best-effort category for a question (section 10's `category` field).

    Sensitivity is checked first because those categories are the ones that
    change behaviour.
    """
    verdict = classify_sensitivity(question)
    if verdict.is_sensitive:
        return verdict.category

    lowered = question.lower()
    checks: tuple[tuple[QuestionCategory, tuple[str, ...]], ...] = (
        (QuestionCategory.EXPERIENCE, ("years of experience", "how many years", "experience with", "worked with")),
        (QuestionCategory.EDUCATION, ("degree", "university", "college", "graduat", "gpa", "cgpa")),
        (QuestionCategory.AVAILABILITY, ("notice period", "when can you", "start date", "available", "last working day", "join")),
        (QuestionCategory.SKILLS, ("proficien", "skill", "rate your", "familiar with", "expertise")),
        (QuestionCategory.LOGISTICS, ("location", "city", "remote", "onsite", "hybrid", "shift", "travel")),
        (QuestionCategory.PERSONAL, ("name", "email", "phone", "linkedin", "portfolio", "github", "address")),
    )
    for category, markers in checks:
        if any(m in lowered for m in markers):
            return category
    return QuestionCategory.OTHER
