"""
Gmail processing.

The master prompt's Gmail section is missing from the document (it jumps from
16, Google Sheets, to 18, Company Career Pages), but Gmail is referenced
throughout — section 28 lists "PROCESS GMAIL" in the acceptance workflow,
section 19 expects interview invitations and rejections on the dashboard, and
section 27 wants it documented. This implements what those references require.

Two jobs:

1. **Classify recruiter mail** and update the matching application's status,
   so the dashboard reflects reality without the user forwarding anything.
2. **Fetch verification codes** on request, for the OTP steps a portal login
   sometimes needs.

Scope is read-only (`gmail.readonly`). Nothing is sent, replied to, deleted or
marked read: this reads the user's private mail and should touch as little of
it as possible.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Application, ApplicationStatus
from ..sources.normalize import normalize_company

logger = logging.getLogger(__name__)

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailClient(Protocol):
    """The slice of the Gmail API this module needs."""

    def list_messages(self, query: str, limit: int) -> list[dict[str, Any]]: ...
    def get_message(self, message_id: str) -> dict[str, Any]: ...


@dataclass(slots=True)
class EmailMessage:
    """A parsed message."""

    id: str
    sender: str
    subject: str
    body: str
    received_at: datetime | None = None

    @property
    def text(self) -> str:
        return f"{self.subject}\n{self.body}"


#: Ordered most-decisive first. A rejection that also says "thank you for
#: applying" is a rejection; checking acknowledgements first would mislabel it.
_CLASSIFIERS: tuple[tuple[ApplicationStatus, tuple[str, ...]], ...] = (
    (
        ApplicationStatus.OFFER,
        (
            r"pleased to offer", r"offer letter", r"we are delighted to offer",
            r"extend(?:ing)? an offer", r"job offer",
        ),
    ),
    (
        ApplicationStatus.REJECTED,
        (
            r"not (?:be )?mov(?:e|ing) forward", r"unfortunately",
            r"decided to (?:move forward |proceed )?with other candidates",
            r"not (?:been )?select(?:ed)?", r"regret to inform",
            r"we will not be pursuing", r"no longer under consideration",
            r"not a (?:good )?(?:fit|match) at this time",
        ),
    ),
    (
        ApplicationStatus.INTERVIEW,
        (
            r"schedule (?:an? )?(?:interview|call|chat)",
            r"invit(?:e|ation) (?:you )?to (?:an? )?interview",
            r"interview (?:invitation|round|scheduled)",
            r"would like to speak with you", r"set up a (?:call|time)",
            r"technical (?:round|assessment|screen)",
            r"availability for (?:a )?(?:call|interview)",
        ),
    ),
    (
        ApplicationStatus.RESPONDED,
        (
            r"your (?:application|profile) (?:has been )?(?:viewed|shortlisted)",
            r"next steps", r"recruiter (?:would like|reached out)",
            r"take(?:-| )home", r"assignment",
        ),
    ),
    (
        ApplicationStatus.VIEWED,
        (
            r"thank(?:s| you) for (?:applying|your application)",
            r"we (?:have )?received your application",
            r"application (?:received|submitted|confirmation)",
        ),
    ),
)

_COMPILED = tuple(
    (status, tuple(re.compile(p, re.IGNORECASE) for p in patterns))
    for status, patterns in _CLASSIFIERS
)

_OTP_PATTERNS = (
    re.compile(r"(?:code|otp|pin)(?:\s+is)?[:\s]+(\d{4,8})", re.IGNORECASE),
    re.compile(r"\b(\d{6})\b(?=[^\d]*(?:is your|verification|code))", re.IGNORECASE),
    re.compile(r"(?:verification code|security code)[:\s]+(\d{4,8})", re.IGNORECASE),
)


def classify(message: EmailMessage) -> ApplicationStatus | None:
    """
    What this email says about an application, if anything.

    Returns None for mail that is not about an application outcome — most of
    the inbox — rather than forcing a guess.
    """
    text = message.text
    for status, patterns in _COMPILED:
        if any(p.search(text) for p in patterns):
            return status
    return None


def extract_otp(message: EmailMessage) -> str | None:
    """
    Pull a verification code out of a message.

    Used only when the user asks for it during a login they are watching.
    Nothing here bypasses a security control — it saves them retyping a code
    that was sent to them (section 12 forbids bypassing, not assisting).
    """
    for pattern in _OTP_PATTERNS:
        if match := pattern.search(message.text):
            return match.group(1)
    return None


@dataclass(slots=True)
class GmailReport:
    processed: int = 0
    matched: int = 0
    updated: int = 0
    unmatched_senders: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.processed} message(s) read, {self.matched} matched to an "
            f"application, {self.updated} status change(s)"
        )


class GmailProcessor:
    """
    Reads recruiter mail and updates application statuses.

    Matching is by company name, normalized the same way discovery normalizes
    it, so "noreply@acme-technologies.com" finds the application to "Acme
    Technologies Pvt Ltd".
    """

    def __init__(self, session: Session, client: GmailClient) -> None:
        self.session = session
        self.client = client

    def process(self, *, days: int = 7, limit: int = 50) -> GmailReport:
        report = GmailReport()
        query = f"newer_than:{days}d -category:promotions"

        try:
            raw_messages = self.client.list_messages(query, limit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not list Gmail messages")
            report.unmatched_senders.append(f"error: {exc}")
            return report

        # Only applications that could still change status are candidates.
        open_applications = list(
            self.session.scalars(
                select(Application).where(
                    Application.status.in_(
                        [
                            ApplicationStatus.SUBMITTED,
                            ApplicationStatus.VIEWED,
                            ApplicationStatus.RESPONDED,
                            ApplicationStatus.INTERVIEW,
                        ]
                    )
                )
            )
        )
        by_company: dict[str, list[Application]] = {}
        for application in open_applications:
            by_company.setdefault(normalize_company(application.company), []).append(application)

        for raw in raw_messages:
            message = self._parse(raw)
            if message is None:
                continue
            report.processed += 1

            status = classify(message)
            if status is None:
                continue

            application = self._match(message, by_company)
            if application is None:
                report.unmatched_senders.append(message.sender)
                continue
            report.matched += 1

            # Never walk an application backwards: an automated "we received
            # your application" arriving after an interview invitation must
            # not undo the interview.
            if _rank(status) > _rank(ApplicationStatus(application.status)):
                application.status = status
                report.updated += 1

        self.session.flush()
        return report

    def _match(
        self, message: EmailMessage, by_company: dict[str, list[Application]]
    ) -> Application | None:
        """
        Find the application this message is about.

        Company name is matched against both the sender's domain and the
        subject line, because recruiter mail arrives from ATS domains
        (greenhouse, lever) at least as often as from the company itself.
        """
        haystack = f"{message.sender} {message.subject}".lower()
        for company, applications in by_company.items():
            if company and company in haystack.replace("-", " ").replace(".", " "):
                # Most recent attempt for that company.
                return max(applications, key=lambda a: a.created_at)
        return None

    @staticmethod
    def _parse(raw: dict[str, Any]) -> EmailMessage | None:
        """Turn a Gmail API payload into an EmailMessage."""
        try:
            payload = raw.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            body = _extract_body(payload)
            received = None
            if ts := raw.get("internalDate"):
                received = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            return EmailMessage(
                id=raw.get("id", ""),
                sender=headers.get("from", ""),
                subject=headers.get("subject", ""),
                body=body,
                received_at=received,
            )
        except Exception as exc:  # noqa: BLE001 - one bad message is not fatal
            logger.debug("Could not parse message: %s", exc)
            return None

    def find_verification_code(self, *, minutes: int = 10) -> str | None:
        """Most recent verification code, for a login the user is watching."""
        try:
            raw_messages = self.client.list_messages("newer_than:1d", 10)
        except Exception:  # noqa: BLE001
            return None
        for raw in raw_messages:
            message = self._parse(raw)
            if message is None:
                continue
            if message.received_at is not None:
                age = (datetime.now(timezone.utc) - message.received_at).total_seconds()
                if age > minutes * 60:
                    continue
            if code := extract_otp(message):
                return code
        return None


#: Outcome ordering, so an application never walks backwards.
_STATUS_RANK: dict[ApplicationStatus, int] = {
    ApplicationStatus.DRAFT: 0,
    ApplicationStatus.PREPARED: 1,
    ApplicationStatus.WAITING_FOR_USER: 1,
    ApplicationStatus.FAILED: 1,
    ApplicationStatus.SUBMITTED: 2,
    ApplicationStatus.VIEWED: 3,
    ApplicationStatus.RESPONDED: 4,
    ApplicationStatus.INTERVIEW: 5,
    # Terminal outcomes rank highest: whichever arrives is the final word.
    ApplicationStatus.REJECTED: 6,
    ApplicationStatus.OFFER: 7,
}


def _rank(status: ApplicationStatus) -> int:
    return _STATUS_RANK.get(status, 0)


def _extract_body(payload: dict[str, Any]) -> str:
    """
    Plain text from a Gmail payload, walking MIME parts.

    Prefers text/plain; falls back to stripping tags from text/html, because
    a great many recruiter mails are HTML-only.
    """
    def decode(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""

    if payload.get("mimeType") == "text/plain":
        return decode(payload.get("body", {}).get("data", ""))

    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            if mime == "text/plain":
                plain.append(decode(data))
            elif mime == "text/html":
                html.append(decode(data))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if html:
        return re.sub(r"<[^>]+>", " ", "\n".join(html))
    return ""


class GoogleGmailClient:
    """Real Gmail client. Read-only scope, built lazily."""

    def __init__(self, credentials_file: str, token_file: str) -> None:
        from .google_auth import build_service

        self._service = build_service(
            "gmail", "v1", credentials_file, token_file, scopes=[GMAIL_READONLY_SCOPE]
        )

    def list_messages(self, query: str, limit: int) -> list[dict[str, Any]]:
        result = (
            self._service.users()
            .messages()
            .list(userId="me", q=query, maxResults=limit)
            .execute()
        )
        return [self.get_message(m["id"]) for m in result.get("messages", [])]

    def get_message(self, message_id: str) -> dict[str, Any]:
        return (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
