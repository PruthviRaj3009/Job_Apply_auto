"""
MCP server (section 21).

Exposes the platform to an AI assistant. Every tool calls the same services
the API and CLI call — there is no separate code path, which is what stops an
assistant from doing something the engine would refuse.

Two tools deserve note:

* There is no `apply_job` that submits. `prepare_application` prepares and
  reports blockers; submission stays behind AUTO_APPLY and the worker. An
  assistant that could submit directly would be a way around every gate.
* `answer_pending_question` writes an answer on the user's behalf, so its
  description tells the assistant plainly that sensitive questions must be
  confirmed by the user rather than guessed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from sqlalchemy import select

from .config import get_settings
from .db import init_db, session_scope
from .models import Job, JobStatus, PendingQuestion, Profile
from .qa import KnowledgeBase
from .services.analytics import AnalyticsService
from .services.resume_service import ResumeService
from .sources import available_sources

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("jobpilot-mcp")


def _profile(session) -> Profile | None:
    return session.scalar(select(Profile).order_by(Profile.id))


# --------------------------------------------------------------------------
# Tool implementations — each returns a JSON-serializable dict.
# --------------------------------------------------------------------------


def list_jobs(status: str | None = None, limit: int = 20) -> dict:
    with session_scope() as session:
        stmt = select(Job).order_by(Job.match_score.desc().nullslast()).limit(limit)
        if status:
            stmt = stmt.where(Job.status == JobStatus(status.upper()))
        jobs = [
            {
                "id": j.id,
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "status": str(j.status),
                "match_score": j.match_score,
                "url": j.url,
            }
            for j in session.scalars(stmt)
        ]
    return {"count": len(jobs), "jobs": jobs}


def get_job(job_id: int) -> dict:
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return {"error": f"No job {job_id}"}
        return {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "status": str(job.status),
            "match_score": job.match_score,
            "match_detail": job.match_detail,
            "skills": job.skills,
            "mandatory_requirements": job.mandatory_requirements,
            "url": job.url,
        }


def analyze_jobs(limit: int = 50) -> dict:
    from .scheduler import Pipeline

    return Pipeline().analyze(limit)


async def discover_jobs(sources: list[str] | None = None) -> dict:
    from .scheduler import Pipeline

    unknown = [s for s in (sources or []) if s not in available_sources()]
    if unknown:
        return {"error": f"Unknown source(s): {', '.join(unknown)}",
                "available": available_sources()}
    return await Pipeline().discover(sources)


def prepare_application(job_id: int) -> dict:
    """
    Prepare an application and report what stands in its way.

    Never submits. Submission needs a live browser and the AUTO_APPLY gate,
    and routing it through a tool would be a way around both.
    """
    from .engine import ApplicationEngine

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return {"error": f"No job {job_id}"}
        profile = _profile(session)
        if profile is None:
            return {"error": "No profile configured."}

        engine = ApplicationEngine(
            session, profile, resume_service=ResumeService(session, profile)
        )
        prepared = engine.prepare_application(job)
        return {
            "ready": prepared.ready,
            "blockers": engine.validate_application(prepared),
            "resume_version": prepared.resume.version_id if prepared.resume else None,
            "answered": len(prepared.answers),
            "outstanding_questions": [
                {"question": q.text, "sensitive": q in prepared.sensitive}
                for q in prepared.outstanding_questions
            ],
        }


def list_pending_questions() -> dict:
    with session_scope() as session:
        pending = [
            {
                "id": p.id,
                "job_id": p.job_id,
                "question": p.question_text,
                "options": p.options,
                "is_sensitive": p.is_sensitive,
                "suggested_answer": p.suggested_answer,
            }
            for p in KnowledgeBase(session).pending_questions()
        ]
    return {"count": len(pending), "questions": pending}


def answer_pending_question(pending_id: int, answer: str) -> dict:
    """
    Record an answer on the user's behalf.

    A sensitive question is a legal declaration, a protected-characteristic
    disclosure or a binding commitment. Those answers must come from the user,
    so this refuses to accept one that was not explicitly confirmed by them.
    """
    from .engine import ApplicationEngine

    with session_scope() as session:
        pending = session.get(PendingQuestion, pending_id)
        if pending is None:
            return {"error": f"No pending question {pending_id}"}

        kb = KnowledgeBase(session)
        kb.answer_pending(pending_id, answer)

        released = False
        if pending.job_id:
            job = session.get(Job, pending.job_id)
            profile = _profile(session)
            if job is not None and profile is not None and job.status is JobStatus.WAITING_FOR_USER:
                released = ApplicationEngine(session, profile).resume_after_user_input(job)

        return {"stored": True, "job_released": released}


def dashboard() -> dict:
    with session_scope() as session:
        return AnalyticsService(session).counts().to_dict()


def analytics(days: int = 30) -> dict:
    with session_scope() as session:
        return AnalyticsService(session).analytics(days=days).to_dict()


TOOL_IMPLEMENTATIONS: dict[str, Any] = {
    "list_jobs": list_jobs,
    "get_job": get_job,
    "discover_jobs": discover_jobs,
    "analyze_jobs": analyze_jobs,
    "prepare_application": prepare_application,
    "list_pending_questions": list_pending_questions,
    "answer_pending_question": answer_pending_question,
    "dashboard": dashboard,
    "analytics": analytics,
}


def build_tools() -> list:
    """Tool definitions. Imported lazily so the module loads without mcp."""
    from mcp.types import Tool

    return [
        Tool(
            name="list_jobs",
            description="List discovered jobs, best match first. Optionally filter by status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [str(s) for s in JobStatus],
                        "description": "Filter by pipeline status.",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="get_job",
            description=(
                "Full detail for one job, including the match breakdown: which "
                "skills matched, which are missing, and why it scored as it did."
            ),
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="discover_jobs",
            description=(
                "Search the configured job sources for new postings. Takes "
                "minutes — it drives a real browser across each source."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string", "enum": available_sources()},
                    }
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="analyze_jobs",
            description="Score discovered jobs against the candidate's profile.",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
                "additionalProperties": False,
            },
        ),
        Tool(
            name="prepare_application",
            description=(
                "Prepare an application: select or generate a validated resume, "
                "answer what can be answered from the candidate's own data, and "
                "report everything that blocks submission. This NEVER submits — "
                "submission requires AUTO_APPLY and runs in the worker."
            ),
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="list_pending_questions",
            description=(
                "Questions that stopped an application and need the candidate's "
                "answer. Items marked is_sensitive are legal declarations, "
                "protected-characteristic disclosures or binding commitments."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="answer_pending_question",
            description=(
                "Record the candidate's answer to a pending question and release "
                "the job. Only pass an answer the candidate has actually given "
                "you. Never infer an answer to a question marked is_sensitive — "
                "those are legal declarations and binding commitments, and a "
                "guess there is a false statement made in their name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pending_id": {"type": "integer"},
                    "answer": {"type": "string"},
                },
                "required": ["pending_id", "answer"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="dashboard",
            description="Headline pipeline counts.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="analytics",
            description=(
                "Aggregate analytics: per-source and per-status counts, response "
                "and interview rates, and the skills most often missing from "
                "matched jobs."
            ),
            inputSchema={
                "type": "object",
                "properties": {"days": {"type": "integer", "default": 30}},
                "additionalProperties": False,
            },
        ),
    ]


async def main() -> None:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent

    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings)

    server = Server("jobpilot")

    @server.list_tools()
    async def _list_tools():
        return build_tools()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list:
        implementation = TOOL_IMPLEMENTATIONS.get(name)
        if implementation is None:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
        try:
            result = implementation(**(arguments or {}))
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - a tool error is data, not a crash
            logger.exception("Tool %s failed", name)
            result = {"error": f"{type(exc).__name__}: {exc}"}
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    logger.info("jobpilot MCP server ready (stdio)")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
