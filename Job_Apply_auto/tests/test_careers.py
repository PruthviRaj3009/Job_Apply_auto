"""Company career-page adapters: ATS detection and board parsing."""

from __future__ import annotations

import json

import pytest

from app.sources import detect_ats, get_adapter
from app.sources.careers.ats import _strip_html


# ================================================================ detection

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/acmecorp", ("greenhouse", "acmecorp")),
        ("https://job-boards.greenhouse.io/globex/jobs/123", ("greenhouse", "globex")),
        ("https://jobs.lever.co/initech", ("lever", "initech")),
        ("https://jobs.lever.co/initech/abc-def-123", ("lever", "initech")),
        ("https://jobs.ashbyhq.com/umbrella", ("ashby", "umbrella")),
        ("https://careers.smartrecruiters.com/Hooli", ("smartrecruiters", "Hooli")),
        ("https://piedpiper.recruitee.com/", ("recruitee", "piedpiper")),
        ("https://apply.workable.com/vehement/", ("workable", "vehement")),
    ],
)
def test_ats_is_detected_from_a_careers_url(url, expected):
    """Adding a company should be as simple as pasting its careers link."""
    assert detect_ats(url) == expected


def test_bespoke_careers_page_is_reported_as_unknown():
    """
    A real outcome, not a failure — the caller falls back to the generic HTML
    adapter rather than pretending it found an ATS.
    """
    assert detect_ats("https://acme.com/careers") is None
    assert detect_ats("") is None


def test_adapter_builds_the_right_api_url():
    adapter = get_adapter("careerpage", {"company": "Acme", "platform": "greenhouse", "slug": "acme"})
    from app.sources import SearchQuery

    url = adapter.build_search_url(SearchQuery())
    assert url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"


def test_career_pages_do_not_claim_to_support_search():
    """
    These APIs return the whole board rather than answering a query, so
    filtering happens on our side.
    """
    assert get_adapter("careerpage").supports_search is False


# ================================================================== parsing

GREENHOUSE = json.dumps({
    "jobs": [
        {
            "id": 4012345,
            "title": "Senior DevOps Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012345",
            "updated_at": "2026-09-15T10:30:00-04:00",
            "location": {"name": "Pune, India"},
            "content": "<p>Own our platform.</p><ul><li>Kubernetes</li><li>Terraform</li></ul>",
        },
        {"id": 2, "title": "", "absolute_url": "https://x/2"},  # unusable
    ]
})

LEVER = json.dumps([
    {
        "id": "abc-123",
        "text": "Platform Engineer",
        "hostedUrl": "https://jobs.lever.co/initech/abc-123",
        "applyUrl": "https://jobs.lever.co/initech/abc-123/apply",
        "createdAt": 1757894400000,
        "categories": {"location": "Remote", "commitment": "Full-time"},
        "descriptionPlain": "Build the platform.",
    }
])

ASHBY = json.dumps({
    "jobs": [
        {
            "id": "xyz",
            "title": "SRE",
            "jobUrl": "https://jobs.ashbyhq.com/umbrella/xyz",
            "location": "Bangalore",
            "publishedAt": "2026-09-10T00:00:00Z",
            "descriptionHtml": "<p>Keep it up.</p>",
        }
    ]
})

RECRUITEE = json.dumps({
    "offers": [
        {
            "id": 77,
            "title": "Cloud Engineer",
            "careers_url": "https://piedpiper.recruitee.com/o/cloud-engineer",
            "city": "Pune",
            "country": "India",
            "published_at": "2026-09-12",
            "description": "<p>Cloud things.</p>",
        }
    ]
})


def adapter(platform: str, company: str = "Acme"):
    return get_adapter("careerpage", {"company": company, "platform": platform, "slug": "acme"})


def test_greenhouse_board_parses():
    jobs = adapter("greenhouse").parse_listing(GREENHOUSE)

    assert len(jobs) == 1  # the title-less entry is dropped
    job = jobs[0]
    assert job.title == "Senior DevOps Engineer"
    assert job.company == "Acme"
    assert job.location == "Pune, India"
    assert job.external_id == "4012345"
    assert job.posted_at is not None
    assert "Kubernetes" in job.description


def test_lever_board_parses_a_bare_array():
    """Lever returns an array, not an envelope object."""
    jobs = adapter("lever", "Initech").parse_listing(LEVER)

    assert len(jobs) == 1
    assert jobs[0].title == "Platform Engineer"
    assert jobs[0].application_url.endswith("/apply")
    assert jobs[0].posted_at is not None


def test_ashby_board_parses():
    jobs = adapter("ashby", "Umbrella").parse_listing(ASHBY)
    assert jobs[0].title == "SRE"
    assert jobs[0].location == "Bangalore"


def test_recruitee_board_parses():
    jobs = adapter("recruitee", "Pied Piper").parse_listing(RECRUITEE)
    assert jobs[0].location == "Pune, India"


def test_career_page_jobs_are_treated_as_drivable():
    """A company's own application flow is native, so the engine can drive it."""
    assert adapter("greenhouse").parse_listing(GREENHOUSE)[0].easy_apply is True


@pytest.mark.parametrize("payload", ["", "not json", "{}", "[]", '{"jobs": null}'])
def test_bad_board_response_yields_nothing(payload):
    """A changed or truncated API response must not kill the discovery run."""
    assert adapter("greenhouse").parse_listing(payload) == []


def test_unknown_platform_yields_nothing():
    assert adapter("some-new-ats").parse_listing(GREENHOUSE) == []


def test_unexpected_payload_shape_is_contained():
    """Each ATS changes its API eventually; that must not raise."""
    assert adapter("greenhouse").parse_listing('{"jobs": [{"id": {}}]}') == []


# ============================================================ html stripping

def test_html_descriptions_keep_their_structure():
    """
    The JD parser needs bullets and line breaks to split requirements from
    responsibilities, so block tags become newlines rather than vanishing.
    """
    text = _strip_html(
        "<h3>Requirements</h3><ul><li>Must have Kubernetes</li>"
        "<li>Terraform required</li></ul><p>Apply now</p>"
    )
    assert "Requirements" in text
    assert text.count("•") == 2
    assert "Must have Kubernetes" in text
    assert "<" not in text


def test_html_entities_are_decoded():
    assert _strip_html("R&amp;D team&nbsp;lead") == "R&D team lead"


def test_stripping_empty_html():
    assert _strip_html("") == ""


def test_stripped_description_feeds_the_jd_parser():
    """End to end: an ATS description must yield usable structure."""
    from app.analysis import parse_jd

    jobs = adapter("greenhouse").parse_listing(GREENHOUSE)
    parsed = parse_jd(jobs[0].description)
    assert "Kubernetes" in parsed.skills
    assert "Terraform" in parsed.skills
