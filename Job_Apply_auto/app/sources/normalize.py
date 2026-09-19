"""
Normalization: turning a `RawJob` from any source into one `Job` shape.

Two things here carry real weight:

**Fingerprinting.** Duplicate prevention (section 30) cannot key on URL —
portals rewrite URLs, append tracking parameters, and repost the same role
under a new ID every few weeks. The fingerprint is derived from company +
title + location, all aggressively normalized, so the same role collapses to
one row however it was found.

**Not guessing.** A parser that cannot read a salary leaves it None. Filling
in a plausible number would put an invented figure into the tracking sheet and
the match score.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from ..models.enums import EmploymentType, WorkMode
from .base import RawJob

# --------------------------------------------------------------------------
# Text normalization
# --------------------------------------------------------------------------

#: Suffixes that are noise when comparing company names.
_COMPANY_SUFFIXES = (
    "private limited", "pvt ltd", "pvt. ltd.", "pvt limited", "limited", "ltd",
    "incorporated", "inc", "llc", "llp", "corporation", "corp", "gmbh", "plc",
    "technologies", "technology", "solutions", "services", "systems",
    "software", "labs", "india", "global",
)

#: Seniority and noise words stripped before comparing titles, so
#: "Senior DevOps Engineer (Remote)" and "DevOps Engineer" match.
_TITLE_NOISE = (
    "senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate",
    "i", "ii", "iii", "iv", "remote", "hybrid", "onsite", "contract",
    "full time", "fulltime", "part time", "urgent", "hiring", "immediate",
    "joiner", "joiners", "opening", "openings", "position", "role",
)


def _collapse(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    text = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def normalize_company(company: str) -> str:
    """
    Reduce a company name to a comparable core.

    "Acme Technologies Pvt. Ltd." and "ACME Technologies" both become "acme".
    Suffixes are stripped repeatedly from the end because real names stack
    them ("Foo Software Solutions Pvt Ltd").
    """
    name = _collapse(company)
    changed = True
    while changed and name:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if name.endswith(" " + suffix) or name == suffix:
                name = name[: -len(suffix)].strip()
                changed = True
    return name


def normalize_title(title: str) -> str:
    """Strip seniority and decoration so equivalent titles compare equal."""
    # Drop bracketed asides: "DevOps Engineer (3-5 yrs, Pune)".
    title = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", title or "")
    tokens = [t for t in _collapse(title).split() if t not in _TITLE_NOISE]
    return " ".join(tokens)


def normalize_location(location: str) -> str:
    """
    Reduce a location to its primary city.

    Portals write "Pune, Maharashtra, India", "Pune/Bangalore" and
    "Remote - India" for what is, for dedupe purposes, the same place.
    """
    if not (location or "").strip():
        return ""
    lowered = location.lower()
    if "remote" in lowered or "work from home" in lowered or "wfh" in lowered:
        return "remote"
    # Split on the ORIGINAL text: _collapse strips the very separators the
    # split needs, which silently turned "Pune, Maharashtra, India" into one
    # token and broke cross-portal fingerprint matching.
    primary = _collapse(re.split(r"[,/|]| and | – | - ", location)[0])
    if not primary:
        return ""
    # Common metro aliases.
    aliases = {
        "bengaluru": "bangalore",
        "gurugram": "gurgaon",
        "new delhi": "delhi",
        "navi mumbai": "mumbai",
        "thane": "mumbai",
        "noida": "noida",
        "hyderabad telangana": "hyderabad",
    }
    return aliases.get(primary, primary)


def job_fingerprint(company: str, title: str, location: str = "") -> str:
    """
    Stable identity for a posting, independent of URL and source.

    Location is included because the same company genuinely opens the same
    role in several cities, and those are different jobs. It is normalized to
    a single city first, so "Pune, MH" and "Pune, India" do not split one job
    into two.
    """
    basis = f"{normalize_company(company)}|{normalize_title(title)}|{normalize_location(location)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# Field parsers
# --------------------------------------------------------------------------

_REMOTE_HINTS = ("remote", "work from home", "wfh", "anywhere", "telecommute")
_HYBRID_HINTS = ("hybrid", "flexible working", "partly remote", "2 days in office", "3 days in office")
_ONSITE_HINTS = ("on-site", "onsite", "in office", "in-office", "work from office", "wfo")


def detect_work_mode(*texts: str) -> WorkMode:
    """
    Infer remote/hybrid/onsite from whatever text is available.

    Hybrid is checked first: a hybrid posting almost always contains the word
    "remote" too, so checking remote first would mislabel most of them.
    """
    blob = " ".join(_collapse(t) for t in texts if t)
    if not blob:
        return WorkMode.UNKNOWN
    if any(h in blob for h in _HYBRID_HINTS):
        return WorkMode.HYBRID
    if any(h in blob for h in _REMOTE_HINTS):
        return WorkMode.REMOTE
    if any(h in blob for h in _ONSITE_HINTS):
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN


_EMPLOYMENT_PATTERNS: tuple[tuple[EmploymentType, tuple[str, ...]], ...] = (
    (EmploymentType.INTERNSHIP, ("intern", "internship", "trainee")),
    (EmploymentType.CONTRACT, ("contract", "contractual", "c2h", "contract to hire", "freelance")),
    (EmploymentType.PART_TIME, ("part time", "part-time")),
    (EmploymentType.TEMPORARY, ("temporary", "temp ", "seasonal")),
    (EmploymentType.FULL_TIME, ("full time", "full-time", "permanent", "regular")),
)


def detect_employment_type(*texts: str) -> EmploymentType:
    """Most specific match wins — 'full time contract' is a contract."""
    blob = " ".join(_collapse(t) for t in texts if t)
    if not blob:
        return EmploymentType.UNKNOWN
    for kind, hints in _EMPLOYMENT_PATTERNS:
        if any(h in blob for h in hints):
            return kind
    return EmploymentType.UNKNOWN


#: "12-18 LPA", "₹8,00,000 - ₹12,00,000", "$120k - $150k", "25 Lacs PA"
_SALARY_RE = re.compile(
    r"(?P<low>\d[\d,.]*)\s*(?P<lowunit>lpa|lakh|lacs?|l|k|cr|crore)?\s*"
    r"(?:-|to|–|—)\s*"
    r"(?P<high>\d[\d,.]*)\s*(?P<highunit>lpa|lakh|lacs?|l|k|cr|crore)?",
    re.IGNORECASE,
)

_UNIT_MULTIPLIER = {
    "lpa": 100_000, "lakh": 100_000, "lac": 100_000, "lacs": 100_000, "l": 100_000,
    "cr": 10_000_000, "crore": 10_000_000,
    "k": 1_000,
}


def parse_salary(text: str) -> tuple[float | None, float | None]:
    """
    Extract a (min, max) salary range in absolute units.

    Returns (None, None) whenever the text cannot be read confidently — an
    unparsed salary must stay unknown rather than become a wrong number in the
    tracking sheet. "Not disclosed" is the most common value on Indian
    portals and correctly yields nothing.
    """
    if not text:
        return None, None
    blob = text.replace("₹", " ").replace("$", " ")
    match = _SALARY_RE.search(blob)
    if not match:
        return None, None
    try:
        low = float(match.group("low").replace(",", ""))
        high = float(match.group("high").replace(",", ""))
    except ValueError:
        return None, None

    # A unit given only on the high end applies to both ("12 - 18 LPA").
    unit = (match.group("highunit") or match.group("lowunit") or "").lower()
    multiplier = _UNIT_MULTIPLIER.get(unit, 1)
    low, high = low * multiplier, high * multiplier
    if low > high:
        low, high = high, low
    return low, high


_EXPERIENCE_RANGE_RE = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*(?:-|to|–|—)\s*(?P<high>\d+(?:\.\d+)?)\s*(?:\+)?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
_EXPERIENCE_MIN_RE = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE
)


def parse_experience(text: str) -> tuple[float | None, float | None]:
    """
    Extract a (min, max) years-of-experience range.

    "3-5 years" -> (3, 5). "5+ years" -> (5, None). Unreadable -> (None, None).
    """
    if not text:
        return None, None
    if m := _EXPERIENCE_RANGE_RE.search(text):
        low, high = float(m.group("low")), float(m.group("high"))
        return (low, high) if low <= high else (high, low)
    if m := _EXPERIENCE_MIN_RE.search(text):
        return float(m.group("low")), None
    return None, None


_RELATIVE_DATE_RE = re.compile(
    r"(?P<n>\d+)\+?\s*(?P<unit>minute|min|hour|hr|day|week|month)s?\s*(?:ago|old)?",
    re.IGNORECASE,
)


def parse_posted_at(text: str, *, now: datetime | None = None) -> datetime | None:
    """
    Turn a relative posting date into an absolute timestamp.

    Portals say "3 days ago", "Just now", "30+ days ago". Storing the absolute
    time means recency stays correct however long a job sits in the queue,
    which a stored "3 days ago" would not.
    """
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    blob = text.lower().strip()

    if any(w in blob for w in ("just now", "just posted", "today", "few seconds", "moments ago")):
        return now
    if "yesterday" in blob:
        return now - timedelta(days=1)

    if m := _RELATIVE_DATE_RE.search(blob):
        n = int(m.group("n"))
        unit = m.group("unit").lower()
        delta = {
            "minute": timedelta(minutes=n),
            "min": timedelta(minutes=n),
            "hour": timedelta(hours=n),
            "hr": timedelta(hours=n),
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n),
        }.get(unit)
        if delta is not None:
            return now - delta

    # Absolute formats some sources emit.
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(blob[: len(fmt) + 2].strip(), fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def days_since(posted_at: datetime | None, *, now: datetime | None = None) -> int | None:
    if posted_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return max(0, (now - posted_at).days)


# --------------------------------------------------------------------------
# RawJob -> Job field dict
# --------------------------------------------------------------------------


def normalize(raw: RawJob, *, now: datetime | None = None) -> dict:
    """
    Produce the column values for a `Job` row from a `RawJob`.

    Returns a plain dict rather than a Job instance so the caller decides
    between inserting and updating — discovery needs both.
    """
    now = now or datetime.now(timezone.utc)

    posted_at = raw.posted_at
    if posted_at is None and raw.posted_days_ago is not None and raw.posted_days_ago >= 0:
        posted_at = now - timedelta(days=raw.posted_days_ago)

    salary_min, salary_max = parse_salary(raw.salary_text)
    exp_min, exp_max = parse_experience(raw.experience_text or raw.description or "")

    work_mode = raw.work_mode
    if work_mode is WorkMode.UNKNOWN:
        work_mode = detect_work_mode(raw.location, raw.title, raw.description)

    employment_type = raw.employment_type
    if employment_type is EmploymentType.UNKNOWN:
        employment_type = detect_employment_type(raw.title, raw.description)

    return {
        "fingerprint": job_fingerprint(raw.company, raw.title, raw.location),
        "external_id": raw.external_id or None,
        "url": raw.url,
        "application_url": raw.application_url or None,
        "title": raw.title,
        "company": raw.company,
        "location": raw.location,
        "work_mode": work_mode,
        "employment_type": employment_type,
        "description": raw.description or None,
        "salary_text": raw.salary_text or None,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "posted_at": posted_at,
        "easy_apply": raw.easy_apply,
        "skills": list(raw.skills),
        "raw": raw.raw or None,
    }
