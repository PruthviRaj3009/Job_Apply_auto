"""Normalization: fingerprints, and parsers that must never guess."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.enums import EmploymentType, WorkMode
from app.sources import (
    RawJob,
    detect_employment_type,
    detect_work_mode,
    job_fingerprint,
    normalize,
    normalize_company,
    normalize_location,
    normalize_title,
    parse_experience,
    parse_posted_at,
    parse_salary,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------- company

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme Technologies Pvt. Ltd.", "acme"),
        ("ACME TECHNOLOGIES", "acme"),
        ("Acme Software Solutions Private Limited", "acme"),
        ("Acme Inc.", "acme"),
        ("Acme", "acme"),
    ],
)
def test_company_suffixes_collapse(raw, expected):
    """
    Stacked legal and filler suffixes all reduce to the same core, so a
    blocked company stays blocked however the portal spells it.
    """
    assert normalize_company(raw) == expected


def test_distinct_companies_stay_distinct():
    assert normalize_company("Acme Ltd") != normalize_company("Globex Ltd")


# ----------------------------------------------------------------- title

@pytest.mark.parametrize(
    "raw",
    [
        "Senior DevOps Engineer",
        "DevOps Engineer",
        "Sr. DevOps Engineer (Remote)",
        "Lead DevOps Engineer - Immediate Joiners",
        "DevOps Engineer II",
    ],
)
def test_seniority_and_decoration_strip_to_one_title(raw):
    assert normalize_title(raw) == "devops engineer"


def test_different_roles_do_not_collapse():
    assert normalize_title("Data Engineer") != normalize_title("DevOps Engineer")


# -------------------------------------------------------------- location

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Pune, Maharashtra, India", "pune"),
        ("Pune", "pune"),
        ("Bengaluru", "bangalore"),
        ("Bangalore, India", "bangalore"),
        ("Gurugram", "gurgaon"),
        ("Remote - India", "remote"),
        ("Work From Home", "remote"),
    ],
)
def test_location_reduces_to_primary_city(raw, expected):
    assert normalize_location(raw) == expected


# ----------------------------------------------------------- fingerprint

def test_same_role_cross_posted_has_one_fingerprint():
    """
    The core duplicate-prevention guarantee: the same job found on three
    portals, spelled three ways, is one job.
    """
    a = job_fingerprint("Acme Technologies Pvt Ltd", "Senior DevOps Engineer", "Pune, Maharashtra")
    b = job_fingerprint("ACME Technologies", "DevOps Engineer", "Pune")
    c = job_fingerprint("Acme", "Sr. DevOps Engineer (Remote)", "Pune, India")
    assert a == b == c


def test_same_role_in_different_cities_is_two_jobs():
    """Genuinely separate openings must not be collapsed."""
    assert job_fingerprint("Acme", "DevOps Engineer", "Pune") != job_fingerprint(
        "Acme", "DevOps Engineer", "Hyderabad"
    )


def test_different_roles_at_one_company_differ():
    assert job_fingerprint("Acme", "DevOps Engineer", "Pune") != job_fingerprint(
        "Acme", "Data Engineer", "Pune"
    )


def test_fingerprint_is_stable_across_calls():
    assert job_fingerprint("Acme", "DevOps Engineer", "Pune") == job_fingerprint(
        "Acme", "DevOps Engineer", "Pune"
    )


# ---------------------------------------------------------------- salary

@pytest.mark.parametrize(
    "text,low,high",
    [
        ("12-18 LPA", 1_200_000, 1_800_000),
        ("12 - 18 LPA", 1_200_000, 1_800_000),
        ("8 to 12 Lacs", 800_000, 1_200_000),
        ("₹8,00,000 - ₹12,00,000", 800_000, 1_200_000),
        ("$120k - $150k", 120_000, 150_000),
    ],
)
def test_salary_ranges_parse(text, low, high):
    assert parse_salary(text) == (low, high)


@pytest.mark.parametrize(
    "text",
    ["Not disclosed", "", "Competitive", "As per industry standards", "Best in class"],
)
def test_unreadable_salary_stays_unknown(text):
    """
    An unparsed salary must be None, never a plausible default — a guessed
    figure would land in the tracking sheet as though it were published.
    """
    assert parse_salary(text) == (None, None)


def test_reversed_salary_range_is_ordered():
    assert parse_salary("18 - 12 LPA") == (1_200_000, 1_800_000)


# ------------------------------------------------------------ experience

@pytest.mark.parametrize(
    "text,expected",
    [
        ("3-5 years", (3.0, 5.0)),
        ("3 to 5 yrs", (3.0, 5.0)),
        ("5+ years experience", (5.0, None)),
        ("Minimum 4 years", (4.0, None)),
        ("No experience details", (None, None)),
        ("", (None, None)),
    ],
)
def test_experience_parsing(text, expected):
    assert parse_experience(text) == expected


# ---------------------------------------------------------------- posted

@pytest.mark.parametrize(
    "text,expected_days",
    [
        ("3 days ago", 3),
        ("1 day ago", 1),
        ("2 weeks ago", 14),
        ("30+ days ago", 30),
        ("Just now", 0),
        ("Today", 0),
        ("Yesterday", 1),
        ("5 hours ago", 0),
    ],
)
def test_relative_dates_become_absolute(text, expected_days):
    """
    Relative text is resolved at parse time. Storing "3 days ago" verbatim
    would silently become wrong the moment the job sat in the queue.
    """
    parsed = parse_posted_at(text, now=NOW)
    assert parsed is not None
    assert (NOW - parsed).days == expected_days


def test_unparseable_date_is_none():
    assert parse_posted_at("sometime recently", now=NOW) is None
    assert parse_posted_at("", now=NOW) is None


# ------------------------------------------------------------- work mode

def test_hybrid_wins_over_remote():
    """
    Hybrid postings nearly always contain the word "remote" too. Checking
    remote first would mislabel most of them.
    """
    assert detect_work_mode("Hybrid - 2 days remote, Pune") is WorkMode.HYBRID


def test_remote_detected():
    assert detect_work_mode("Remote (India)") is WorkMode.REMOTE
    assert detect_work_mode("Work From Home") is WorkMode.REMOTE


def test_onsite_detected():
    assert detect_work_mode("Work from office, Pune") is WorkMode.ONSITE


def test_work_mode_unknown_when_silent():
    assert detect_work_mode("Pune, Maharashtra") is WorkMode.UNKNOWN
    assert detect_work_mode("") is WorkMode.UNKNOWN


def test_contract_beats_full_time():
    """'Full time contract' is a contract — the more specific match wins."""
    assert detect_employment_type("Full time contract role") is EmploymentType.CONTRACT


def test_internship_detected():
    assert detect_employment_type("Software Engineering Intern") is EmploymentType.INTERNSHIP


# ------------------------------------------------------------- normalize

def test_normalize_produces_job_columns():
    raw = RawJob(
        title="Senior DevOps Engineer",
        url="https://naukri.com/job/123",
        source_slug="naukri",
        company="Acme Technologies Pvt Ltd",
        location="Pune, Maharashtra",
        salary_text="12-18 LPA",
        experience_text="3-5 years",
        description="Hybrid role. Kubernetes and Terraform.",
        posted_days_ago=3,
        easy_apply=True,
        skills=["Kubernetes", "Terraform"],
    )
    fields = normalize(raw, now=NOW)

    assert fields["title"] == "Senior DevOps Engineer"
    assert fields["salary_min"] == 1_200_000
    assert fields["experience_min_years"] == 3.0
    assert fields["work_mode"] is WorkMode.HYBRID
    assert fields["easy_apply"] is True
    assert fields["skills"] == ["Kubernetes", "Terraform"]
    assert (NOW - fields["posted_at"]).days == 3
    assert fields["fingerprint"] == job_fingerprint(
        "Acme Technologies Pvt Ltd", "Senior DevOps Engineer", "Pune, Maharashtra"
    )


def test_normalize_leaves_absent_fields_empty():
    """A source that published nothing must not produce invented values."""
    raw = RawJob(title="DevOps Engineer", url="https://x.example/1", source_slug="x")
    fields = normalize(raw, now=NOW)

    assert fields["salary_min"] is None
    assert fields["salary_max"] is None
    assert fields["posted_at"] is None
    assert fields["description"] is None
    assert fields["experience_min_years"] is None
