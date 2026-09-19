"""
Adapter parsing, against fixtures rather than live portals.

Section 22 asks for mocked pages and forbids touching real applications. The
adapters are built so the parsers are pure, which is what lets these run with
no browser and no network.
"""

from __future__ import annotations

import json

import pytest

from app.sources import SearchQuery, available_sources, get_adapter, is_registered
from app.sources.portals.card_adapter import CardScrapeAdapter
from app.sources.portals.linkedin import LinkedInAdapter
from app.sources.portals.naukri import NaukriAdapter

# --------------------------------------------------------------- registry

EXPECTED_SOURCES = {
    "linkedin", "naukri", "wellfound", "indeed",
    "hirist", "glassdoor", "instahyre", "cutshort",
}


def test_all_portals_from_section_4_are_registered():
    assert EXPECTED_SOURCES.issubset(set(available_sources()))


def test_unknown_source_raises_with_a_useful_message():
    with pytest.raises(KeyError) as exc:
        get_adapter("monster")
    assert "naukri" in str(exc.value)  # lists what IS available


@pytest.mark.parametrize("slug", sorted(EXPECTED_SOURCES))
def test_every_adapter_builds_a_search_url(slug):
    adapter = get_adapter(slug)
    url = adapter.build_search_url(
        SearchQuery(keywords=["DevOps Engineer"], location="Pune", experience_years=4)
    )
    assert url.startswith("http")
    assert " " not in url  # properly encoded


def test_linkedin_uses_server_side_recency_filter():
    """
    f_TPR is LinkedIn's own "past N seconds" window. Filtering after the fact
    is unreliable because the scraper only sees the first page or two.
    """
    url = LinkedInAdapter().build_search_url(SearchQuery(keywords=["SRE"], max_days_old=1))
    assert "f_TPR=r86400" in url


def test_linkedin_easy_apply_filter_is_opt_in():
    q = SearchQuery(keywords=["SRE"])
    assert "f_AL" not in LinkedInAdapter().build_search_url(q)
    q.easy_apply_only = True
    assert "f_AL=true" in LinkedInAdapter().build_search_url(q)


# ----------------------------------------------------------------- naukri

NAUKRI_RESPONSE = json.dumps(
    {
        "jobDetails": [
            {
                "jobId": "010101000001",
                "title": "Senior DevOps Engineer",
                "companyName": "Acme Technologies",
                "jdURL": "/job-listings-senior-devops-engineer-acme-010101000001",
                "createdDate": 1758124800000,  # ms epoch
                "companyApplyJob": False,
                "tagsAndSkills": "Kubernetes,Terraform,AWS",
                "jobDescription": "Own the platform. Kubernetes, Terraform.",
                "placeholders": [
                    {"type": "location", "label": "Pune, Maharashtra"},
                    {"type": "salary", "label": "12-18 LPA"},
                    {"type": "experience", "label": "3-5 Yrs"},
                ],
            },
            {
                "jobId": "010101000002",
                "title": "Cloud Engineer",
                "companyName": "Globex",
                "jdURL": "https://www.naukri.com/job-listings-cloud-engineer-globex-2",
                "createdDate": 1758124800,  # seconds epoch
                "companyApplyJob": True,  # external apply
                "placeholders": [{"type": "location", "label": "Remote"}],
            },
        ]
    }
)


def test_naukri_parses_its_api_response():
    jobs = NaukriAdapter().parse_listing(NAUKRI_RESPONSE)
    assert len(jobs) == 2

    first = jobs[0]
    assert first.title == "Senior DevOps Engineer"
    assert first.company == "Acme Technologies"
    assert first.location == "Pune, Maharashtra"
    assert first.salary_text == "12-18 LPA"
    assert first.skills == ["Kubernetes", "Terraform", "AWS"]
    assert first.url.startswith("https://www.naukri.com/")


def test_naukri_marks_external_apply_as_not_easy_apply():
    """
    companyApplyJob=True means "apply on the company's own site" — not a
    Naukri-native flow the engine can drive.
    """
    jobs = NaukriAdapter().parse_listing(NAUKRI_RESPONSE)
    assert jobs[0].easy_apply is True
    assert jobs[1].easy_apply is False


def test_naukri_handles_both_epoch_scales():
    """createdDate arrives in seconds or milliseconds depending on endpoint."""
    jobs = NaukriAdapter().parse_listing(NAUKRI_RESPONSE)
    assert jobs[0].posted_at is not None
    assert jobs[1].posted_at is not None
    assert jobs[0].posted_at.year == jobs[1].posted_at.year == 2025


@pytest.mark.parametrize("payload", ["", "not json", "[]", "{}", '{"jobDetails": null}'])
def test_naukri_survives_a_bad_response(payload):
    """A changed or truncated response must yield nothing, not raise."""
    assert NaukriAdapter().parse_listing(payload) == []


def test_naukri_skips_entries_missing_title_or_url():
    payload = json.dumps({"jobDetails": [{"companyName": "Acme"}, {"title": "X"}]})
    assert NaukriAdapter().parse_listing(payload) == []


# --------------------------------------------------------------- linkedin

LINKEDIN_CARDS = json.dumps(
    [
        {
            "title": "DevOps Engineer",
            "company": "Acme",
            "location": "Pune, Maharashtra, India",
            "href": "/jobs/view/devops-engineer-at-acme-4012345678?refId=abc&trk=xyz",
            "posted": "3 days ago",
        },
        {"title": "", "company": "Ghost", "location": "", "href": "/jobs/view/1", "posted": ""},
        {"title": "SRE", "company": "Globex", "location": "Remote", "href": "", "posted": "1 week ago"},
    ]
)


def test_linkedin_parses_cards_and_strips_tracking_params():
    """
    Tracking parameters differ between sightings of the same job; leaving
    them on would defeat URL-level deduplication during a scroll.
    """
    jobs = LinkedInAdapter().parse_listing(LINKEDIN_CARDS)
    assert len(jobs) == 1  # the blank-title and missing-href rows are dropped

    job = jobs[0]
    assert job.url == "https://www.linkedin.com/jobs/view/devops-engineer-at-acme-4012345678"
    assert "?" not in job.url
    assert job.external_id == "4012345678"
    assert job.posted_at is not None


def test_linkedin_parse_handles_garbage():
    assert LinkedInAdapter().parse_listing("not json") == []
    assert LinkedInAdapter().parse_listing('{"not": "a list"}') == []


# ------------------------------------------------------------ html cards

WELLFOUND_HTML = """
<html><body>
  <div class="JobSearchResult">
    <a class="jobTitle" href="/jobs/1-devops-engineer">Senior DevOps Engineer</a>
    <a class="company">Acme Technologies</a>
    <span class="location">Pune, India</span>
    <span class="salary">12-18 LPA</span>
  </div>
  <div class="JobSearchResult">
    <a class="jobTitle" href="https://wellfound.com/jobs/2">Platform Engineer</a>
    <a class="company">Globex</a>
    <span class="location">Remote</span>
  </div>
  <div class="JobSearchResult">
    <span class="location">No title here</span>
  </div>
</body></html>
"""


def test_card_adapter_parses_html_and_skips_incomplete_cards():
    jobs = get_adapter("wellfound").parse_listing(WELLFOUND_HTML)
    assert len(jobs) == 2

    first = jobs[0]
    assert first.title == "Senior DevOps Engineer"
    assert first.company == "Acme Technologies"
    assert first.salary_text == "12-18 LPA"
    # Relative href resolved against the portal's base URL.
    assert first.url == "https://wellfound.com/jobs/1-devops-engineer"
    # Absolute href left alone.
    assert jobs[1].url == "https://wellfound.com/jobs/2"


def test_card_adapter_leaves_missing_salary_empty_not_placeholder():
    """
    The old scrapers wrote the literal string "Not listed", which the salary
    parser would then try to read. Absent means empty.
    """
    jobs = get_adapter("wellfound").parse_listing(WELLFOUND_HTML)
    assert jobs[1].salary_text == ""


@pytest.mark.parametrize("html", ["", "   ", "<html></html>", "<<<broken"])
def test_card_adapter_survives_unusable_html(html):
    assert get_adapter("wellfound").parse_listing(html) == []


def test_card_adapter_respects_max_cards():
    many = "<html><body>" + (
        '<div class="JobSearchResult">'
        '<a class="jobTitle" href="/j/{i}">Job {i}</a></div>'
    ).replace("{i}", "x") * 60 + "</body></html>"
    adapter = get_adapter("wellfound")
    assert len(adapter.parse_listing(many)) <= adapter.max_cards


@pytest.mark.parametrize(
    "slug", ["wellfound", "indeed", "hirist", "glassdoor", "instahyre", "cutshort"]
)
def test_html_portals_share_one_scrape_implementation(slug):
    """
    The six card portals were six copies of the same loop. They are now one
    implementation plus a selector spec — this guards that consolidation.
    """
    adapter = get_adapter(slug)
    assert isinstance(adapter, CardScrapeAdapter)
    assert adapter.selectors.card and adapter.selectors.title


def test_application_url_falls_back_to_posting_url():
    adapter = get_adapter("naukri")
    jobs = adapter.parse_listing(NAUKRI_RESPONSE)
    assert adapter.get_application_url(jobs[0]) == jobs[0].url


def test_is_registered():
    assert is_registered("linkedin")
    assert not is_registered("nope")
