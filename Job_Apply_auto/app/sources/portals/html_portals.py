"""
The six HTML-card portals: Wellfound, Indeed, Hirist, Glassdoor, Instahyre,
Cutshort.

Each is a selector spec plus its search-URL shape. The selectors are carried
over from the working scrapers in job-apply-mcp, including their fallback
alternatives — those were earned against the live sites and are worth keeping
verbatim.
"""

from __future__ import annotations

import urllib.parse

from ..base import SearchQuery
from ..registry import register
from .card_adapter import CardScrapeAdapter, CardSelectors


@register
class WellfoundAdapter(CardScrapeAdapter):
    slug = "wellfound"
    display_name = "Wellfound"
    base_url = "https://wellfound.com"
    selectors = CardSelectors(
        card=[
            "div[class*='JobSearchResult']",
            "div[class*='job-listing']",
            "div[data-test='JobListing']",
        ],
        title=["a[class*='jobTitle']", "h2 a", "a[data-test='job-title']"],
        link=["a[class*='jobTitle']", "h2 a", "a[data-test='job-title']"],
        company=["a[class*='company']", "h2[class*='company']", "a[data-test='startup-link']"],
        location=["span[class*='location']", "span[data-test='location']"],
        salary=["span[class*='salary']", "span[data-test='compensation']"],
        posted=["span[class*='posted']", "time"],
    )

    def build_search_url(self, query: SearchQuery) -> str:
        params = {"q": query.keyword_string, "location": query.location or "India"}
        if query.remote_only:
            params["remote"] = "true"
        return f"{self.base_url}/jobs?" + urllib.parse.urlencode(params)


@register
class IndeedAdapter(CardScrapeAdapter):
    slug = "indeed"
    display_name = "Indeed India"
    base_url = "https://in.indeed.com"
    selectors = CardSelectors(
        card=["div.job_seen_beacon", "div.jobsearch-SerpJobCard", "td.resultContent"],
        title=["h2.jobTitle a", "a[data-jk]", "span[title]"],
        link=["h2.jobTitle a", "a[data-jk]"],
        company=["span[data-testid='company-name']", "span.companyName", "span.company"],
        location=["div[data-testid='text-location']", "div.companyLocation", "span.location"],
        salary=[
            "div.salary-snippet-container",
            "span.salary-snippet",
            "div.metadata.salary-snippet-container",
        ],
        posted=["span.date", "span[data-testid='myJobsStateDate']"],
    )

    def build_search_url(self, query: SearchQuery) -> str:
        params = {
            "q": query.keyword_string,
            "l": "Remote" if query.remote_only else (query.location or "India"),
            # Indeed's own recency filter tops out at 14 days.
            "fromage": str(min(query.max_days_old, 14)),
        }
        return f"{self.base_url}/jobs?" + urllib.parse.urlencode(params)


@register
class HiristAdapter(CardScrapeAdapter):
    slug = "hirist"
    display_name = "Hirist"
    base_url = "https://www.hirist.tech"
    selectors = CardSelectors(
        card=["div[class*='jobCard']", "div.job-listing", "div[class*='job-tuple']"],
        title=["a[class*='jobTitle']", "h2 a", "a.title"],
        link=["a[class*='jobTitle']", "h2 a", "a.title"],
        company=["span[class*='company']", "div[class*='companyName']"],
        location=["span[class*='location']", "div[class*='location']"],
        salary=["span[class*='salary']", "div[class*='salary']"],
        experience=["span[class*='experience']", "div[class*='exp']"],
        posted=["span[class*='posted']", "span[class*='date']"],
    )

    def build_search_url(self, query: SearchQuery) -> str:
        params = {
            "q": query.keyword_string,
            "loc": query.location or "India",
            "exp": str(int(query.experience_years or 0)),
        }
        return f"{self.base_url}/jobs?" + urllib.parse.urlencode(params)


@register
class GlassdoorAdapter(CardScrapeAdapter):
    slug = "glassdoor"
    display_name = "Glassdoor India"
    base_url = "https://www.glassdoor.co.in"
    request_delay = 6.0  # Glassdoor rate-limits aggressively.
    selectors = CardSelectors(
        card=["li[data-test='jobListing']", "div.react-job-listing", "li.JobsList_jobListItem__*"],
        title=["a[data-test='job-title']", "a.jobLink", "div[class*='jobTitle'] a"],
        link=["a[data-test='job-title']", "a.jobLink"],
        company=["span[class*='EmployerProfile_compactEmployerName']", "div[class*='employerName']"],
        location=["div[data-test='emp-location']", "div[class*='location']"],
        salary=["div[data-test='detailSalary']", "span[class*='salaryEstimate']"],
        posted=["div[data-test='job-age']", "div[class*='listingAge']"],
    )

    def build_search_url(self, query: SearchQuery) -> str:
        params = {"sc.keyword": query.keyword_string, "locT": "N", "locId": "115"}
        if query.location:
            params["locKeyword"] = query.location
        return f"{self.base_url}/Job/jobs.htm?" + urllib.parse.urlencode(params)


@register
class InstahyreAdapter(CardScrapeAdapter):
    slug = "instahyre"
    display_name = "Instahyre"
    base_url = "https://www.instahyre.com"
    selectors = CardSelectors(
        card=["div.job-card", "div[class*='opportunity']", "div[ng-repeat*='job']"],
        title=["a.job-title", "h2 a", "a[class*='title']"],
        link=["a.job-title", "h2 a", "a[class*='title']"],
        company=["div.company-name", "span[class*='company']", "a[class*='employer']"],
        location=["div.job-location", "span[class*='location']"],
        salary=["div[class*='salary']", "span[class*='ctc']"],
        experience=["div[class*='experience']", "span[class*='exp']"],
    )

    def build_search_url(self, query: SearchQuery) -> str:
        params = {
            "q": query.keyword_string,
            "location": query.location or "India",
            "experience": str(int(query.experience_years or 0)),
        }
        return f"{self.base_url}/search-jobs/?" + urllib.parse.urlencode(params)


@register
class CutshortAdapter(CardScrapeAdapter):
    slug = "cutshort"
    display_name = "Cutshort"
    base_url = "https://cutshort.io"
    selectors = CardSelectors(
        card=["div[class*='JobCard']", "div[class*='job-card']", "article[class*='job']"],
        title=["a[class*='jobTitle']", "h3 a", "h2 a"],
        link=["a[class*='jobTitle']", "h3 a", "h2 a"],
        company=["a[class*='companyName']", "div[class*='company']"],
        location=["span[class*='location']", "div[class*='location']"],
        salary=["span[class*='salary']", "div[class*='compensation']"],
        experience=["span[class*='experience']"],
    )

    def build_search_url(self, query: SearchQuery) -> str:
        exp = int(query.experience_years or 0)
        params = {
            "q": query.keyword_string,
            "location": query.location or "India",
            "experience": f"{exp}-{exp + 2}",
        }
        return f"{self.base_url}/jobs?" + urllib.parse.urlencode(params)
