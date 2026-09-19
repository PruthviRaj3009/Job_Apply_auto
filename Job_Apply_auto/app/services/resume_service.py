"""
Resume service: decide, generate, compile, validate, version (sections 7-9).

Implements the section 7 workflow end to end:

    JD -> requirements -> compare with master profile + existing resume ->
    identify gaps -> verify the user truly has the experience ->
    tailor if truthfully supported -> LaTeX -> PDF -> validate -> use

The verification step is not advisory. A resume that fails validation is
never marked usable, so the application engine cannot attach it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.events import log_event
from ..models import (
    EventType,
    Job,
    Profile,
    ResumeKind,
    ResumeVersion,
)
from ..resume import (
    GapAnalyzer,
    GapReport,
    ResumeGenerator,
    compile_tex,
    latex_available,
    validate_pdf,
    validate_tex,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResumeDecision:
    """Whether to tailor, and why."""

    tailor: bool
    reason: str
    gap_report: GapReport


class ResumeService:
    def __init__(
        self,
        session: Session,
        profile: Profile,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.profile = profile
        self.settings = settings or get_settings()
        self.generator = ResumeGenerator()

    # ------------------------------------------------------------ decision

    def decide(self, job: Job) -> ResumeDecision:
        """
        Should this job get its own resume? (section 7)

        Tailoring is only worthwhile when there is something truthful to
        surface. If the master resume already states everything the job asks
        for that the candidate genuinely has, generating a near-identical
        variant just adds a version to track.
        """
        master = self.master_resume()
        resume_text = self._resume_text(master)

        keywords = self._job_keywords(job)
        report = GapAnalyzer(self.profile, resume_text).analyze(keywords)

        if not keywords:
            return ResumeDecision(False, "No keywords could be extracted from the job.", report)
        if report.should_tailor():
            return ResumeDecision(True, report.tailoring_reason(), report)
        return ResumeDecision(
            False,
            "Existing resume already covers everything this job asks for that "
            "your profile supports.",
            report,
        )

    @staticmethod
    def _job_keywords(job: Job) -> list[str]:
        """Skills the job wants, from the analysis already stored on it."""
        keywords = list(job.skills or [])
        detail = job.match_detail or {}
        for key in ("missing_skills", "matching_skills"):
            for skill in detail.get(key) or []:
                if skill not in keywords:
                    keywords.append(skill)
        return keywords

    # ---------------------------------------------------------- generation

    def generate_for_job(self, job: Job, *, force: bool = False) -> ResumeVersion | None:
        """
        Produce a job-specific resume, or return the master when tailoring is
        not warranted.

        Returns None only when no usable resume could be produced at all —
        the application engine treats that as a reason to stop rather than to
        attach something unvalidated.
        """
        decision = self.decide(job)
        if not decision.tailor and not force:
            logger.info("Job %s: %s", job.id, decision.reason)
            return self.master_resume()

        log_event(
            self.session,
            EventType.RESUME_TAILORING_STARTED,
            job_id=job.id,
            message=decision.reason,
            payload={"keywords": [v.to_dict() for v in decision.gap_report.verdicts]},
        )

        generated = self.generator.generate(self.profile, gap_report=decision.gap_report)

        version_id = self._version_id(job)
        output_dir = Path(self.settings.resume_output_dir) / version_id
        output_dir.mkdir(parents=True, exist_ok=True)

        record = ResumeVersion(
            version_id=version_id,
            kind=ResumeKind.JOB_SPECIFIC_RESUME,
            profile_id=self.profile.id,
            job_id=job.id,
            keywords_emphasized=generated.keywords_emphasized,
            keywords_omitted=generated.keywords_omitted,
            supporting_evidence=generated.supporting_evidence,
            tailoring_reason=generated.tailoring_reason,
            generated_at=datetime.now(timezone.utc),
        )

        # --- validate the source before spending a compile on it ---------
        source_report = validate_tex(
            generated.tex,
            self.profile,
            expected_links=generated.links,
            gap_report=decision.gap_report,
        )

        tex_path = output_dir / f"{version_id}.tex"
        tex_path.write_text(generated.tex, encoding="utf-8")
        record.tex_path = str(tex_path)

        if not source_report.ok:
            # A resume that asserts something unsupported must never be used,
            # whatever else is true of it.
            record.validation_report = source_report.to_dict()
            record.validated = False
            self.session.add(record)
            self.session.flush()
            logger.error(
                "Resume %s failed source validation: %s", version_id, source_report.errors
            )
            return None

        log_event(
            self.session,
            EventType.RESUME_GENERATED,
            job_id=job.id,
            message=f"Generated {version_id}",
            payload={"emphasized": generated.keywords_emphasized,
                     "omitted": generated.keywords_omitted},
        )

        # --- compile ------------------------------------------------------
        combined = source_report
        if latex_available(self.settings.latex_command):
            compiled = compile_tex(
                generated.tex,
                output_dir,
                stem=version_id,
                command=self.settings.latex_command,
            )
            record.compile_log = compiled.log[-8000:] if compiled.log else None
            record.compiled = compiled.ok
            record.page_count = compiled.page_count

            if compiled.ok and compiled.pdf_path is not None:
                record.pdf_path = str(compiled.pdf_path)
                pdf_report = validate_pdf(
                    compiled.pdf_path,
                    self.profile,
                    page_count=compiled.page_count,
                    expected_links=generated.links,
                )
                combined.errors.extend(pdf_report.errors)
                combined.warnings.extend(pdf_report.warnings)
                combined.checks_run.extend(pdf_report.checks_run)
                log_event(
                    self.session,
                    EventType.RESUME_COMPILED,
                    job_id=job.id,
                    message=compiled.summary,
                )
            else:
                combined.errors.extend(compiled.errors)
        else:
            # No toolchain is a real limitation, not a validation failure. The
            # .tex is still produced and the source checks still ran.
            combined.warnings.append(
                f"{self.settings.latex_command} is not installed; no PDF was produced. "
                "The LaTeX source was generated and validated."
            )

        record.validation_report = combined.to_dict()
        record.validated = combined.ok and record.compiled
        self.session.add(record)
        self.session.flush()

        if not combined.ok:
            logger.error("Resume %s failed validation: %s", version_id, combined.errors)
            return None
        return record

    # ------------------------------------------------------------- master

    def master_resume(self) -> ResumeVersion | None:
        """The current master resume, if one has been generated."""
        return self.session.scalar(
            select(ResumeVersion)
            .where(
                ResumeVersion.profile_id == self.profile.id,
                ResumeVersion.kind == ResumeKind.MASTER_RESUME,
            )
            .order_by(ResumeVersion.created_at.desc())
        )

    def generate_master(self) -> ResumeVersion:
        """
        Generate (or regenerate) the master resume.

        Untailored: everything in natural order. This is the baseline the gap
        analysis compares against, so it must exist before tailoring can tell
        "absent from the resume" from "already there".
        """
        generated = self.generator.generate(self.profile, gap_report=None)
        version_id = self._next_master_version_id()
        output_dir = Path(self.settings.resume_output_dir) / version_id
        output_dir.mkdir(parents=True, exist_ok=True)

        tex_path = output_dir / f"{version_id}.tex"
        tex_path.write_text(generated.tex, encoding="utf-8")

        record = ResumeVersion(
            version_id=version_id,
            kind=ResumeKind.MASTER_RESUME,
            profile_id=self.profile.id,
            tex_path=str(tex_path),
            tailoring_reason=generated.tailoring_reason,
            generated_at=datetime.now(timezone.utc),
        )

        report = validate_tex(generated.tex, self.profile, expected_links=generated.links)

        if latex_available(self.settings.latex_command):
            compiled = compile_tex(
                generated.tex, output_dir, stem=version_id,
                command=self.settings.latex_command,
            )
            record.compiled = compiled.ok
            record.page_count = compiled.page_count
            record.compile_log = compiled.log[-8000:] if compiled.log else None
            if compiled.ok and compiled.pdf_path is not None:
                record.pdf_path = str(compiled.pdf_path)
                pdf_report = validate_pdf(
                    compiled.pdf_path, self.profile,
                    page_count=compiled.page_count, expected_links=generated.links,
                )
                report.errors.extend(pdf_report.errors)
                report.warnings.extend(pdf_report.warnings)
            else:
                report.errors.extend(compiled.errors)
        else:
            report.warnings.append(f"{self.settings.latex_command} is not installed; no PDF produced.")

        record.validation_report = report.to_dict()
        record.validated = report.ok and record.compiled
        self.session.add(record)
        self.session.flush()
        return record

    # ------------------------------------------------------------ helpers

    def _resume_text(self, version: ResumeVersion | None) -> str:
        """
        The current resume's text, for the under-represented check.

        Reads the .tex rather than the PDF: it is the authoritative source and
        needs no extraction step that can silently return nothing.
        """
        if version is None or not version.tex_path:
            return ""
        try:
            return Path(version.tex_path).read_text(encoding="utf-8")
        except OSError:
            logger.warning("Could not read resume source at %s", version.tex_path)
            return ""

    def _version_id(self, job: Job) -> str:
        """
        Human-readable version id, e.g. resume_devops_engineer_2026_09_19_v1.

        Readable because it appears in the tracking sheet and in filenames the
        user opens by hand; the counter makes regeneration non-destructive.
        """
        slug = re.sub(r"[^a-z0-9]+", "_", (job.title or "role").lower()).strip("_")[:40]
        stamp = date.today().strftime("%Y_%m_%d")
        stem = f"resume_{slug}_{stamp}"

        existing = self.session.scalars(
            select(ResumeVersion.version_id).where(ResumeVersion.version_id.like(f"{stem}_v%"))
        ).all()
        return f"{stem}_v{len(existing) + 1}"

    def _next_master_version_id(self) -> str:
        existing = self.session.scalars(
            select(ResumeVersion.version_id).where(ResumeVersion.version_id.like("resume_master_v%"))
        ).all()
        return f"resume_master_v{len(existing) + 1}"
