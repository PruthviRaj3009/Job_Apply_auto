"""
Loading the master profile from a JSON file.

The profile is the truthfulness anchor for the whole system: a generated
resume may assert nothing this file does not contain, and an application
question may be answered only from it. So loading is strict — an unreadable
file or a missing required field fails loudly rather than producing a
half-populated profile that silently limits what can be said.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Certification,
    Education,
    Experience,
    Profile,
    Project,
    Skill,
)

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = ("full_name", "email")


class ProfileFormatError(ValueError):
    """The profile file is not usable."""


def _parse_date(value: Any) -> date | None:
    """
    Accept the date shapes people actually write.

    A month-precision date ("2022-01") is normalised to the first of the
    month; guessing a day is harmless for a resume date range, unlike
    guessing a whole date, which is why a missing value stays None.
    """
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return date.fromisoformat(text) if fmt == "%Y-%m-%d" else _strptime(text, fmt)
        except ValueError:
            continue
    # Month precision.
    for fmt in ("%Y-%m", "%m/%Y", "%b %Y", "%B %Y"):
        try:
            return _strptime(text, fmt).replace(day=1)
        except ValueError:
            continue
    logger.warning("Could not parse date %r; leaving it unset", value)
    return None


def _strptime(text: str, fmt: str) -> date:
    from datetime import datetime

    return datetime.strptime(text, fmt).date()


def load_profile(session: Session, data: dict) -> Profile:
    """
    Create or replace the master profile from a dict.

    Replaces rather than merges: a partial merge would leave stale skills and
    roles behind, and "why does my resume still claim X?" is a bad question to
    have to debug. The file is the whole truth.
    """
    missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
    if missing:
        raise ProfileFormatError(
            f"Profile is missing required field(s): {', '.join(missing)}"
        )

    existing = session.scalar(select(Profile).order_by(Profile.id))
    if existing is not None:
        session.delete(existing)
        session.flush()

    profile = Profile(
        full_name=data["full_name"],
        email=data["email"],
        phone=data.get("phone", ""),
        location=data.get("location", ""),
        headline=data.get("headline", ""),
        summary=data.get("summary", ""),
        linkedin_url=data.get("linkedin_url"),
        github_url=data.get("github_url"),
        portfolio_url=data.get("portfolio_url"),
        other_links=data.get("other_links", {}),
        total_experience_years=float(data.get("total_experience_years", 0) or 0),
        notice_period_days=data.get("notice_period_days"),
        current_ctc=data.get("current_ctc"),
        expected_ctc=data.get("expected_ctc"),
        target_roles=data.get("target_roles", []),
        search_keywords=data.get("search_keywords", []),
        preferred_locations=data.get("preferred_locations", []),
        excluded_keywords=data.get("excluded_keywords", []),
        excluded_companies=data.get("excluded_companies", []),
        preferences=data.get("preferences", {}),
    )
    session.add(profile)
    session.flush()

    for order, entry in enumerate(data.get("skills", [])):
        if isinstance(entry, str):
            entry = {"name": entry}
        session.add(
            Skill(
                profile_id=profile.id,
                name=entry["name"],
                category=entry.get("category", "Technical"),
                years=entry.get("years"),
                proficiency=entry.get("proficiency", ""),
                # Evidence is what lets a skill be surfaced on a tailored
                # resume, so it is worth recording even informally.
                evidence=entry.get("evidence", ""),
                aliases=entry.get("aliases", []),
            )
        )

    for order, entry in enumerate(data.get("experience", []) or data.get("experiences", [])):
        session.add(
            Experience(
                profile_id=profile.id,
                company=entry.get("company", ""),
                title=entry.get("title", ""),
                location=entry.get("location", ""),
                start_date=_parse_date(entry.get("start_date")),
                end_date=_parse_date(entry.get("end_date")),
                is_current=bool(entry.get("is_current", False)),
                is_internship=bool(entry.get("is_internship", False)),
                bullets=entry.get("bullets", []),
                technologies=entry.get("technologies", []),
                sort_order=order,
            )
        )

    for order, entry in enumerate(data.get("education", []) or data.get("educations", [])):
        session.add(
            Education(
                profile_id=profile.id,
                institution=entry.get("institution", ""),
                degree=entry.get("degree", ""),
                field_of_study=entry.get("field_of_study", ""),
                start_date=_parse_date(entry.get("start_date")),
                end_date=_parse_date(entry.get("end_date")),
                grade=entry.get("grade", ""),
                sort_order=order,
            )
        )

    for order, entry in enumerate(data.get("projects", [])):
        session.add(
            Project(
                profile_id=profile.id,
                name=entry.get("name", ""),
                description=entry.get("description", ""),
                bullets=entry.get("bullets", []),
                technologies=entry.get("technologies", []),
                # Real links, carried into every generated resume unchanged.
                url=entry.get("url"),
                repo_url=entry.get("repo_url"),
                sort_order=order,
            )
        )

    for order, entry in enumerate(data.get("certifications", [])):
        if isinstance(entry, str):
            entry = {"name": entry}
        session.add(
            Certification(
                profile_id=profile.id,
                name=entry["name"],
                issuer=entry.get("issuer", ""),
                issued_date=_parse_date(entry.get("issued_date")),
                expires_date=_parse_date(entry.get("expires_date")),
                credential_id=entry.get("credential_id"),
                credential_url=entry.get("credential_url"),
                sort_order=order,
            )
        )

    session.flush()
    session.refresh(profile)
    return profile


def load_profile_file(session: Session, path: Path) -> Profile:
    path = Path(path)
    if not path.is_file():
        raise ProfileFormatError(f"No profile file at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileFormatError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileFormatError(f"{path} must contain a JSON object")
    return load_profile(session, data)
