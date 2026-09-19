"""
Preference filtering (section 5).

Runs before analysis so the expensive steps — full JD fetch, embeddings, LLM
matching — are never spent on a job the user has already ruled out.

Every rejection carries a reason. A job silently vanishing from the pipeline
is the hardest kind of bug to notice, so `explain()` records why.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from ..models.enums import EmploymentType, WorkMode
from .base import RawJob, SearchQuery
from .normalize import (
    _collapse,
    days_since,
    normalize_company,
    parse_experience,
    parse_salary,
)


@dataclass(slots=True)
class FilterVerdict:
    keep: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.keep


KEEP = FilterVerdict(True)


def _drop(reason: str) -> FilterVerdict:
    return FilterVerdict(False, reason)


def matches_excluded_keyword(text: str, excluded: Sequence[str]) -> str | None:
    """Return the first excluded keyword present, or None."""
    blob = _collapse(text)
    for kw in excluded:
        if kw and _collapse(kw) in blob:
            return kw
    return None


def matches_excluded_company(company: str, excluded: Sequence[str]) -> str | None:
    """
    Company exclusion compares normalized names.

    A user who blocks "Acme" means every posting from "Acme Technologies Pvt
    Ltd" too; a raw string compare would let those straight through.
    """
    target = normalize_company(company)
    if not target:
        return None
    for blocked in excluded:
        norm = normalize_company(blocked)
        if norm and (norm == target or norm in target or target in norm):
            return blocked
    return None


def evaluate(
    job: RawJob,
    query: SearchQuery,
    *,
    now: datetime | None = None,
) -> FilterVerdict:
    """
    Decide whether a discovered job survives the user's stated preferences.

    Unknown values never cause a rejection. A portal that did not publish a
    salary must not be treated as "below your minimum" — that would discard
    most Indian listings, where salary is usually undisclosed.
    """
    now = now or datetime.now(timezone.utc)

    if not job.is_usable:
        return _drop("missing title or URL")

    text = f"{job.title} {job.description}"

    if hit := matches_excluded_keyword(text, query.excluded_keywords):
        return _drop(f"excluded keyword: {hit}")

    if hit := matches_excluded_company(job.company, query.excluded_companies):
        return _drop(f"excluded company: {hit}")

    if query.companies:
        wanted = {normalize_company(c) for c in query.companies}
        if normalize_company(job.company) not in wanted:
            return _drop("company not in target list")

    # --- recency -------------------------------------------------------
    age = job.posted_days_ago
    if age is None or age < 0:
        age = days_since(job.posted_at, now=now)
    if age is not None and query.max_days_old and age > query.max_days_old:
        return _drop(f"posted {age}d ago, older than {query.max_days_old}d")

    # --- easy apply ----------------------------------------------------
    if query.easy_apply_only and not job.easy_apply:
        return _drop("not easy-apply")

    # --- work mode -----------------------------------------------------
    if query.remote_only and job.work_mode not in (WorkMode.REMOTE, WorkMode.UNKNOWN):
        return _drop(f"work mode is {job.work_mode}, remote required")

    # --- employment type -----------------------------------------------
    if query.employment_types and job.employment_type is not EmploymentType.UNKNOWN:
        if job.employment_type not in query.employment_types:
            return _drop(f"employment type {job.employment_type} not wanted")

    # --- experience -----------------------------------------------------
    if query.experience_years is not None:
        exp_min, _ = parse_experience(job.experience_text or job.description or "")
        # Only reject when the job demands clearly more than the candidate
        # has. A 1-year overshoot is within normal hiring latitude.
        if exp_min is not None and exp_min > query.experience_years + 1:
            return _drop(f"requires {exp_min}+ yrs, candidate has {query.experience_years}")

    # --- salary ---------------------------------------------------------
    if query.salary_min is not None:
        _, sal_max = parse_salary(job.salary_text)
        if sal_max is not None and sal_max < query.salary_min:
            return _drop(f"max salary {sal_max:.0f} below minimum {query.salary_min:.0f}")

    return KEEP


def apply_filters(
    jobs: Sequence[RawJob],
    query: SearchQuery,
    *,
    now: datetime | None = None,
) -> tuple[list[RawJob], list[tuple[RawJob, str]]]:
    """
    Split jobs into (kept, [(dropped, reason)]).

    Both halves are returned so the dashboard can show what was filtered and
    why — a discovery run that quietly yields nothing is indistinguishable
    from a broken scraper otherwise.
    """
    kept: list[RawJob] = []
    dropped: list[tuple[RawJob, str]] = []
    for job in jobs:
        verdict = evaluate(job, query, now=now)
        if verdict.keep:
            kept.append(job)
        else:
            dropped.append((job, verdict.reason))
    return kept, dropped
