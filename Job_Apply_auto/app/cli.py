"""
Command-line interface (section 21).

Every command calls the same services the API and MCP server call. Nothing
here decides anything — which is the point: a safety rule enforced in the
engine cannot be sidestepped by using the CLI instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer
from sqlalchemy import select

from .config import get_settings
from .db import init_db, session_scope
from .models import Application, Job, JobStatus, Profile
from .qa import KnowledgeBase
from .resume import latex_available
from .scheduler import Pipeline, ScheduleConfig, Scheduler, recover_stuck
from .services.analytics import AnalyticsService
from .services.resume_service import ResumeService
from .sources import available_sources

app = typer.Typer(
    help="AI job search and application automation.",
    no_args_is_help=True,
    add_completion=False,
)
jobs_app = typer.Typer(help="Inspect discovered jobs.", no_args_is_help=True)
questions_app = typer.Typer(help="Answer questions the engine paused on.", no_args_is_help=True)
resume_app = typer.Typer(help="Generate and inspect resumes.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")
app.add_typer(questions_app, name="questions")
app.add_typer(resume_app, name="resume")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)


def _profile(session) -> Profile:
    profile = session.scalar(select(Profile).order_by(Profile.id))
    if profile is None:
        typer.secho(
            "No profile configured. Run `jobpilot profile import <file.json>` first — "
            "matching and resume generation both read from it.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    return profile


# ============================================================== setup

@app.command()
def init() -> None:
    """Create the database and required directories."""
    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings)
    typer.secho(f"Initialised at {settings.data_dir}", fg=typer.colors.GREEN)


@app.command()
def status() -> None:
    """Show where everything stands."""
    settings = get_settings()
    with session_scope(settings) as session:
        counts = AnalyticsService(session).counts()

    typer.secho("\n  Pipeline", bold=True)
    for label, value in counts.to_dict().items():
        typer.echo(f"    {label.replace('_', ' ').title():<24} {value}")

    typer.secho("\n  Safety", bold=True)
    typer.echo(f"    {'Dry run':<24} {settings.dry_run}")
    typer.echo(f"    {'Auto apply':<24} {settings.auto_apply}")
    typer.echo(f"    {'LaTeX available':<24} {latex_available(settings.latex_command)}")

    if counts.action_required:
        typer.secho(
            f"\n  {counts.action_required} question(s) need your answer — "
            "run `jobpilot questions list`.",
            fg=typer.colors.YELLOW,
        )
    typer.echo()


@app.command("profile")
def import_profile(
    path: Path = typer.Argument(..., help="JSON file describing the candidate."),
) -> None:
    """
    Load the master profile from a JSON file.

    The profile is the only source of truth about the candidate — nothing
    else in the system may assert anything it does not contain.
    """
    from .services.profile_loader import load_profile_file

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as session:
        profile = load_profile_file(session, path)
        typer.secho(
            f"Loaded profile for {profile.full_name}: "
            f"{len(profile.skills)} skills, {len(profile.experiences)} roles, "
            f"{len(profile.projects)} projects.",
            fg=typer.colors.GREEN,
        )
        stored = KnowledgeBase(session, settings=settings).seed_from_profile(profile)
        typer.echo(f"Pre-answered {stored} common application question(s).")


# =========================================================== pipeline

@app.command()
def discover(
    sources: list[str] = typer.Option(None, "--source", "-s", help="Limit to these sources."),
) -> None:
    """Search the configured sources for new jobs."""
    unknown = [s for s in (sources or []) if s not in available_sources()]
    if unknown:
        typer.secho(f"Unknown source(s): {', '.join(unknown)}", fg=typer.colors.RED)
        typer.echo(f"Available: {', '.join(available_sources())}")
        raise typer.Exit(1)

    result = asyncio.run(Pipeline().discover(list(sources) if sources else None))
    typer.echo(json.dumps(result, indent=2))


@app.command()
def analyze(limit: int = typer.Option(50, help="Maximum jobs to score.")) -> None:
    """Score discovered jobs against your profile."""
    typer.echo(json.dumps(Pipeline().analyze(limit), indent=2))


@app.command()
def apply(
    limit: int = typer.Option(None, help="Maximum applications this run."),
) -> None:
    """
    Prepare applications for matched jobs.

    Nothing is submitted unless AUTO_APPLY is on and DRY_RUN is off. With the
    defaults, this produces tailored resumes and surfaces the questions that
    need answering, and stops there.
    """
    settings = get_settings()
    if settings.dry_run or not settings.auto_apply:
        typer.secho(
            "  Dry run: applications will be prepared but not submitted "
            f"(DRY_RUN={settings.dry_run}, AUTO_APPLY={settings.auto_apply}).",
            fg=typer.colors.YELLOW,
        )
    typer.echo(json.dumps(asyncio.run(Pipeline().apply_to_matched(limit)), indent=2))


@app.command()
def run(
    discovery_interval: int = typer.Option(1800, help="Seconds between discovery runs."),
    processing_interval: int = typer.Option(3600, help="Seconds between processing runs."),
) -> None:
    """Run the scheduler until interrupted."""
    config = ScheduleConfig(
        discovery_interval=discovery_interval, processing_interval=processing_interval
    )
    scheduler = Scheduler(config)
    try:
        asyncio.run(scheduler.run())
    except KeyboardInterrupt:
        typer.echo("\nStopped.")


@app.command()
def recover() -> None:
    """Return jobs abandoned by an interrupted run to a runnable state."""
    typer.echo(f"Recovered {recover_stuck()} job(s).")


@app.command()
def login(source: str = typer.Argument(..., help="Source to log in to.")) -> None:
    """
    Open a browser so you can log in yourself.

    OTP, MFA and CAPTCHA steps are yours to complete — nothing here bypasses
    them. The session is saved in the browser profile and survives restarts.
    """
    from .browser import interactive_login

    urls = {
        "linkedin": "https://www.linkedin.com/login",
        "naukri": "https://www.naukri.com/mnjuser/login",
        "indeed": "https://secure.indeed.com/auth",
        "wellfound": "https://wellfound.com/login",
        "hirist": "https://www.hirist.tech/login",
        "glassdoor": "https://www.glassdoor.co.in/profile/login_input.htm",
        "instahyre": "https://www.instahyre.com/login/",
        "cutshort": "https://cutshort.io/login",
    }
    if source not in urls:
        typer.secho(f"No login URL for {source!r}.", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(json.dumps(asyncio.run(interactive_login(source, urls[source])), indent=2))


# =============================================================== jobs

@jobs_app.command("list")
def list_jobs(
    status_filter: str = typer.Option(None, "--status", help="Filter by job status."),
    limit: int = typer.Option(20),
) -> None:
    with session_scope() as session:
        stmt = select(Job).order_by(Job.match_score.desc().nullslast()).limit(limit)
        if status_filter:
            stmt = stmt.where(Job.status == JobStatus(status_filter.upper()))
        rows = list(session.scalars(stmt))

    if not rows:
        typer.echo("No jobs.")
        return
    for job in rows:
        score = f"{job.match_score:.2f}" if job.match_score is not None else "  — "
        typer.echo(f"  [{score}] {job.status:<16} {job.title[:44]:<44} {job.company[:24]}")


@jobs_app.command("show")
def show_job(job_id: int) -> None:
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            typer.secho(f"No job {job_id}.", fg=typer.colors.RED)
            raise typer.Exit(1)

        typer.secho(f"\n  {job.title} — {job.company}", bold=True)
        typer.echo(f"  {job.location}  |  {job.status}  |  {job.url}\n")
        if job.match_detail:
            detail = job.match_detail
            typer.echo(f"  {detail.get('match_explanation', '')}\n")
            if detail.get("missing_skills"):
                typer.echo(f"  Missing: {', '.join(detail['missing_skills'])}")
            for concern in detail.get("concerns", []):
                typer.secho(f"  ! {concern}", fg=typer.colors.YELLOW)
        typer.echo()


# ========================================================== questions

@questions_app.command("list")
def list_questions() -> None:
    """Questions the engine stopped on."""
    with session_scope() as session:
        pending = KnowledgeBase(session).pending_questions()

    if not pending:
        typer.secho("Nothing waiting on you.", fg=typer.colors.GREEN)
        return

    for item in pending:
        marker = typer.style(" SENSITIVE ", bg=typer.colors.YELLOW, fg=typer.colors.BLACK) \
            if item.is_sensitive else ""
        typer.echo(f"\n  [{item.id}] {item.question_text} {marker}")
        if item.options:
            typer.echo(f"        options: {', '.join(item.options)}")
        if item.suggested_answer:
            typer.echo(f"        suggestion: {item.suggested_answer}")
    typer.echo()


@questions_app.command("answer")
def answer_question(
    pending_id: int = typer.Argument(..., help="Id from `questions list`."),
    answer: str = typer.Argument(..., help="Your answer."),
) -> None:
    """
    Answer a pending question and release the job.

    The answer is stored, so the same question on another portal will not stop
    you again.
    """
    from .engine import ApplicationEngine

    with session_scope() as session:
        kb = KnowledgeBase(session)
        from .models import PendingQuestion

        pending = session.get(PendingQuestion, pending_id)
        if pending is None:
            typer.secho(f"No pending question {pending_id}.", fg=typer.colors.RED)
            raise typer.Exit(1)

        kb.answer_pending(pending_id, answer)
        typer.secho("Stored.", fg=typer.colors.GREEN)

        if pending.job_id:
            job = session.get(Job, pending.job_id)
            if job is not None and job.status is JobStatus.WAITING_FOR_USER:
                if ApplicationEngine(session, _profile(session)).resume_after_user_input(job):
                    typer.secho(f"Job {job.id} released back into the pipeline.",
                                fg=typer.colors.GREEN)


# ============================================================= resume

@resume_app.command("master")
def generate_master() -> None:
    """Generate the master resume."""
    with session_scope() as session:
        version = ResumeService(session, _profile(session)).generate_master()
        typer.secho(f"Generated {version.version_id}", fg=typer.colors.GREEN)
        typer.echo(f"  LaTeX: {version.tex_path}")
        if version.pdf_path:
            typer.echo(f"  PDF:   {version.pdf_path} ({version.page_count} page(s))")
        for warning in (version.validation_report or {}).get("warnings", []):
            typer.secho(f"  ! {warning}", fg=typer.colors.YELLOW)
        for error in (version.validation_report or {}).get("errors", []):
            typer.secho(f"  x {error}", fg=typer.colors.RED)


@resume_app.command("for-job")
def generate_for_job(job_id: int) -> None:
    """Generate a tailored resume for one job, if tailoring is warranted."""
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            typer.secho(f"No job {job_id}.", fg=typer.colors.RED)
            raise typer.Exit(1)

        service = ResumeService(session, _profile(session))
        decision = service.decide(job)
        typer.echo(f"  {decision.reason}")
        if not decision.tailor:
            return

        version = service.generate_for_job(job)
        if version is None:
            typer.secho("  Resume failed validation and will not be used.", fg=typer.colors.RED)
            raise typer.Exit(1)
        typer.secho(f"  Generated {version.version_id}", fg=typer.colors.GREEN)
        if version.keywords_omitted:
            typer.echo(f"  Omitted (not supported by your profile): "
                       f"{', '.join(version.keywords_omitted)}")


@resume_app.command("list")
def list_resumes(limit: int = typer.Option(20)) -> None:
    from .models import ResumeVersion

    with session_scope() as session:
        rows = list(
            session.scalars(
                select(ResumeVersion).order_by(ResumeVersion.created_at.desc()).limit(limit)
            )
        )
    for version in rows:
        mark = "ok " if version.validated else "not validated"
        typer.echo(f"  {version.version_id:<48} {mark}")


# ========================================================= integrations

@app.command()
def sync() -> None:
    """Push applications to the tracking spreadsheet."""
    settings = get_settings()
    if not settings.google_sheet_id:
        typer.secho("GOOGLE_SHEET_ID is not set.", fg=typer.colors.RED)
        raise typer.Exit(1)

    from .integrations.sheets import GoogleSheetsClient, SheetsSync

    with session_scope(settings) as session:
        client = GoogleSheetsClient(
            str(settings.gmail_credentials_file), str(settings.gmail_token_file)
        )
        typer.echo(SheetsSync(session, client, settings.google_sheet_id).sync().summary())


@app.command()
def inbox(days: int = typer.Option(7, help="How far back to read.")) -> None:
    """Read recruiter mail and update application statuses."""
    from .integrations.gmail import GmailProcessor, GoogleGmailClient

    settings = get_settings()
    with session_scope(settings) as session:
        client = GoogleGmailClient(
            str(settings.gmail_credentials_file), str(settings.gmail_token_file)
        )
        typer.echo(GmailProcessor(session, client).process(days=days).summary())


@app.command()
def serve(
    host: str = typer.Option(None), port: int = typer.Option(None),
) -> None:
    """Run the API server."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.api.main:app",
        host=host or settings.api_host,
        port=port or settings.api_port,
        log_level=settings.log_level.lower(),
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
