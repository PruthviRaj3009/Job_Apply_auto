"""
Resume validation (section 9).

Checks a generated resume before it is ever attached to an application. The
checks split into two kinds, and the distinction is the whole point:

* **Errors** block use of the resume. A lost GitHub link, a missing contact
  detail, an omitted section, a fabricated keyword.
* **Warnings** are reported and do not block. A two-page resume, an
  unverifiable link.

The link and contact checks exist because those failures are silent. A resume
compiles perfectly with the GitHub URL dropped, and nobody notices until an
employer cannot find the candidate's work.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Profile
from .gap_analysis import GapReport, KeywordStatus

logger = logging.getLogger(__name__)

#: Above this, the resume is long enough to be worth flagging.
PREFERRED_MAX_PAGES = 1


@dataclass(slots=True)
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "checks_run": self.checks_run,
        }

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return f"All {len(self.checks_run)} checks passed"
        parts = []
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        return ", ".join(parts)


#: Sections a resume must not silently lose. Achievements and publications are
#: genuinely optional, so they are not listed.
REQUIRED_SECTIONS = ("Skills", "Experience", "Education")

_URL_RE = re.compile(r"https?://[^\s{}\\]+")


def validate_tex(
    tex: str,
    profile: Profile,
    *,
    expected_links: list[str] | None = None,
    gap_report: GapReport | None = None,
) -> ValidationReport:
    """
    Validate the LaTeX source, before compilation.

    Source-level checking catches the failures that matter most — a dropped
    link, a fabricated keyword — and catches them without needing a LaTeX
    toolchain installed, so they are enforced even on a machine that cannot
    produce a PDF.
    """
    report = ValidationReport()

    # --- contact details ------------------------------------------------
    report.checks_run.append("contact_details")
    if profile.full_name and profile.full_name not in tex:
        report.errors.append(f"Candidate name {profile.full_name!r} is missing from the resume")
    if profile.email and profile.email not in tex:
        report.errors.append(f"Email {profile.email!r} is missing from the resume")
    if profile.phone and _digits(profile.phone) and _digits(profile.phone) not in _digits(tex):
        report.warnings.append("Phone number is missing from the resume")

    # --- real links preserved -------------------------------------------
    # Identity links (LinkedIn, GitHub, portfolio) must always survive.
    # Project and certificate links are only obliged to appear for the items
    # the resume actually shows — trimming to the most relevant projects is
    # legitimate, dropping the link from a project that IS shown is not. The
    # generator reports what it rendered, which is what `expected_links` is.
    report.checks_run.append("links_preserved")
    for url in _identity_links(profile):
        if url not in tex:
            report.errors.append(f"Link dropped from resume: {url}")

    for url in expected_links or []:
        if url not in tex:
            report.errors.append(f"Expected link missing from resume: {url}")

    # --- required sections ----------------------------------------------
    report.checks_run.append("required_sections")
    for name in REQUIRED_SECTIONS:
        if not _has_content(profile, name):
            continue
        if f"\\section{{{name}}}" not in tex:
            report.errors.append(f"Section {name!r} is missing although the profile has content for it")

    # --- truthfulness ---------------------------------------------------
    # The decisive check: nothing the gap analysis ruled unsupported may have
    # reached the document. This is what makes section 30 enforceable rather
    # than aspirational.
    report.checks_run.append("no_unsupported_keywords")
    if gap_report is not None:
        lowered = tex.lower()
        for verdict in gap_report.absent:
            if re.search(rf"(?<![\w/]){re.escape(verdict.keyword.lower())}(?![\w/])", lowered):
                report.errors.append(
                    f"Unsupported keyword {verdict.keyword!r} appears in the resume "
                    "but is not evidenced anywhere in the master profile"
                )
        for verdict in gap_report.transferable:
            if re.search(rf"(?<![\w/]){re.escape(verdict.keyword.lower())}(?![\w/])", lowered):
                report.errors.append(
                    f"Transferable-only keyword {verdict.keyword!r} appears in the resume. "
                    f"Only the real adjacent skill ({verdict.via_skill!r}) may be stated."
                )

    # --- structural sanity ----------------------------------------------
    report.checks_run.append("latex_structure")
    if "\\begin{document}" not in tex or "\\end{document}" not in tex:
        report.errors.append("LaTeX document structure is incomplete")
    if "%%" in tex:
        unfilled = set(re.findall(r"%%([A-Z_]+)%%", tex))
        if unfilled:
            report.errors.append(f"Template placeholders were not filled: {', '.join(sorted(unfilled))}")

    report.checks_run.append("malformed_characters")
    for marker in ("\x00", "�"):
        if marker in tex:
            report.errors.append("Resume contains malformed characters")
            break

    return report


def validate_pdf(
    pdf_path: Path,
    profile: Profile,
    *,
    page_count: int | None = None,
    expected_links: list[str] | None = None,
) -> ValidationReport:
    """
    Validate the compiled PDF.

    Text is extracted with a deliberately simple reader and the result is used
    only to *confirm* things are present — never to fail a resume because
    extraction was imperfect. A compressed PDF may yield no readable text at
    all, which is a limitation of the check, not a defect in the resume.
    """
    report = ValidationReport()

    report.checks_run.append("pdf_exists")
    if not pdf_path.exists():
        report.errors.append(f"PDF was not produced at {pdf_path}")
        return report
    if pdf_path.stat().st_size == 0:
        report.errors.append("PDF is empty")
        return report

    report.checks_run.append("page_count")
    if page_count is not None:
        if page_count == 0:
            report.errors.append("PDF contains no pages")
        elif page_count > PREFERRED_MAX_PAGES:
            report.warnings.append(
                f"Resume is {page_count} pages; one page is preferred for most roles"
            )

    report.checks_run.append("pdf_text_extraction")
    text = extract_pdf_text(pdf_path)
    if not text:
        report.warnings.append(
            "Could not extract text from the PDF to cross-check its contents. "
            "The source-level checks still apply."
        )
        return report

    if profile.full_name and profile.full_name.split()[0] not in text:
        report.warnings.append("Candidate name was not found in the extracted PDF text")
    for url in expected_links or _identity_links(profile):
        stripped = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
        if stripped and stripped not in text:
            report.warnings.append(f"Link not found in extracted PDF text: {url}")

    return report


def extract_pdf_text(pdf_path: Path) -> str:
    """
    Best-effort text extraction from uncompressed PDF streams.

    Deliberately minimal: it reads text-showing operators out of plain
    streams. Anything it cannot read yields "", and callers treat that as
    unknown rather than as a failure.
    """
    try:
        data = pdf_path.read_bytes()
    except OSError:
        return ""

    chunks: list[str] = []
    for match in re.finditer(rb"\((?:\\.|[^()\\])*\)", data):
        raw = match.group(0)[1:-1]
        try:
            chunks.append(raw.decode("latin-1"))
        except UnicodeDecodeError:
            continue
    return " ".join(chunks)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _identity_links(profile: Profile) -> list[str]:
    """
    Links that must appear on every resume regardless of tailoring.

    These identify the candidate rather than illustrating one piece of work,
    so no amount of trimming justifies losing them (section 30).
    """
    links = [
        url
        for url in (profile.linkedin_url, profile.github_url, profile.portfolio_url)
        if url
    ]
    links.extend(url for url in (profile.other_links or {}).values() if url)
    return list(dict.fromkeys(links))


def _has_content(profile: Profile, section_name: str) -> bool:
    return {
        "Skills": bool(profile.skills),
        "Experience": bool(profile.experiences),
        "Education": bool(profile.educations),
        "Projects": bool(profile.projects),
        "Certifications": bool(profile.certifications),
    }.get(section_name, False)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")
