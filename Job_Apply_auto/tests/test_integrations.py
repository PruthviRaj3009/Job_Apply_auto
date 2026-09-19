"""Google Sheets sync and Gmail processing, against fake clients."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations import COLUMNS, SheetsSync, build_row, classify, extract_otp, row_key
from app.integrations.gmail import EmailMessage, GmailProcessor
from app.models import Application, ApplicationStatus

from .conftest import make_job


# ================================================================= Sheets

class FakeSheets:
    """In-memory stand-in for the Sheets API."""

    def __init__(self) -> None:
        self.header: list[str] = []
        self.rows: list[list[str]] = []

    def read_column(self, sheet_id, column):
        index = COLUMNS.index(column)
        return [row[index] if index < len(row) else "" for row in self.rows]

    def write_header(self, sheet_id, header):
        self.header = header

    def append_row(self, sheet_id, values):
        self.rows.append(values)

    def update_row(self, sheet_id, row_number, values):
        self.rows[row_number - 2] = values


@pytest.fixture()
def application(session):
    job = make_job(session, fingerprint="sheet", title="DevOps Engineer", company="Acme",
                   location="Pune", match_detail={"matching_skills": ["Kubernetes"],
                                                  "missing_skills": ["Kafka"],
                                                  "match_explanation": "Strong match"})
    app = Application(
        job_id=job.id, company="Acme", job_title="DevOps Engineer", source="naukri",
        job_url="https://naukri.com/j/1", match_score=0.85,
        status=ApplicationStatus.SUBMITTED, resume_version="resume_master_v1",
        submitted_at=datetime.now(timezone.utc),
    )
    session.add(app)
    session.flush()
    return app


def test_header_matches_the_specified_columns(session, application):
    sheets = FakeSheets()
    SheetsSync(session, sheets, "sheet-1").sync()
    assert sheets.header == list(COLUMNS)


def test_row_carries_the_application_detail(session, application):
    from app.models import Job

    row = build_row(application, session.get(Job, application.job_id))
    as_dict = dict(zip(COLUMNS, row))
    assert as_dict["Company"] == "Acme"
    assert as_dict["Job Title"] == "DevOps Engineer"
    assert as_dict["Match Score"] == "0.85"
    assert as_dict["Matching Skills"] == "Kubernetes"
    assert as_dict["Missing Skills"] == "Kafka"
    assert as_dict["Location"] == "Pune"
    assert as_dict["Resume Version"] == "resume_master_v1"


def test_first_sync_creates_a_row(session, application):
    sheets = FakeSheets()
    report = SheetsSync(session, sheets, "s").sync()

    assert report.created == 1
    assert len(sheets.rows) == 1


def test_resync_updates_in_place(session, application):
    """
    Section 16: update existing rows rather than creating duplicates. The old
    tracker appended blindly, so a job retried four times became four rows.
    """
    sheets = FakeSheets()
    sync = SheetsSync(session, sheets, "s")
    sync.sync()

    application.status = ApplicationStatus.INTERVIEW
    session.flush()
    report = sync.sync()

    assert report.updated == 1
    assert report.created == 0
    assert len(sheets.rows) == 1
    assert dict(zip(COLUMNS, sheets.rows[0]))["Status"] == "INTERVIEW"


def test_row_key_is_per_attempt(session, application):
    """
    A job legitimately has several attempts and collapsing them would hide a
    retry history the user wants to see.
    """
    second = Application(
        job_id=application.job_id, company="Acme", job_title="DevOps Engineer", attempt=2
    )
    session.add(second)
    session.flush()
    assert row_key(application) != row_key(second)


def test_one_bad_row_does_not_stop_the_sync(session, application, monkeypatch):
    sheets = FakeSheets()
    good = Application(job_id=application.job_id, company="Globex", job_title="SRE")
    session.add(good)
    session.flush()

    calls = {"n": 0}
    original = sheets.append_row

    def flaky(sheet_id, values):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient sheets error")
        original(sheet_id, values)

    sheets.append_row = flaky
    report = SheetsSync(session, sheets, "s").sync()

    assert report.created == 1
    assert len(report.errors) == 1


def test_unreachable_sheet_reports_rather_than_raises(session, application):
    class Broken(FakeSheets):
        def write_header(self, sheet_id, header):
            raise RuntimeError("network down")

    report = SheetsSync(session, Broken(), "s").sync()
    assert report.created == 0
    assert any("unreachable" in e for e in report.errors)


# ================================================================== Gmail

def msg(subject: str, body: str = "", sender: str = "recruiter@acme.com") -> EmailMessage:
    return EmailMessage(id="1", sender=sender, subject=subject, body=body,
                        received_at=datetime.now(timezone.utc))


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("Thank you for applying to Acme", ApplicationStatus.VIEWED),
        ("We received your application", ApplicationStatus.VIEWED),
        ("Next steps for your application", ApplicationStatus.RESPONDED),
        ("Let's schedule an interview", ApplicationStatus.INTERVIEW),
        ("Invitation to interview at Acme", ApplicationStatus.INTERVIEW),
        ("We are pleased to offer you the position", ApplicationStatus.OFFER),
        ("Unfortunately we will not be moving forward", ApplicationStatus.REJECTED),
        ("We regret to inform you", ApplicationStatus.REJECTED),
    ],
)
def test_recruiter_mail_is_classified(subject, expected):
    assert classify(msg(subject)) is expected


def test_rejection_beats_a_polite_acknowledgement():
    """
    Almost every rejection also thanks you for applying. Checking
    acknowledgements first would label them all as VIEWED.
    """
    email = msg(
        "Your application to Acme",
        "Thank you for applying. Unfortunately we will not be moving forward.",
    )
    assert classify(email) is ApplicationStatus.REJECTED


def test_unrelated_mail_is_not_classified():
    """Most of an inbox is not about applications, and must not be forced."""
    assert classify(msg("Your Amazon order has shipped")) is None
    assert classify(msg("Team lunch on Friday")) is None


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Your verification code is 483920", "483920"),
        ("OTP: 12345", "12345"),
        ("Use security code: 9081 to continue", "9081"),
    ],
)
def test_otp_extraction(body, expected):
    assert extract_otp(msg("Verify your account", body)) == expected


def test_no_otp_returns_none():
    assert extract_otp(msg("Hello", "Nothing numeric here")) is None


class FakeGmail:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages

    def list_messages(self, query, limit):
        return self.messages[:limit]

    def get_message(self, message_id):
        return next(m for m in self.messages if m["id"] == message_id)


def gmail_payload(subject: str, body: str, sender: str, message_id: str = "1") -> dict:
    return {
        "id": message_id,
        "internalDate": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    }


def test_gmail_updates_a_matching_application(session, application):
    gmail = FakeGmail([
        gmail_payload("Interview invitation", "Let's schedule an interview",
                      "talent@acme.com")
    ])
    report = GmailProcessor(session, gmail).process()

    assert report.matched == 1
    assert report.updated == 1
    assert application.status is ApplicationStatus.INTERVIEW


def test_company_matching_survives_ats_domains(session, application):
    """
    Recruiter mail arrives from greenhouse/lever at least as often as from the
    company, so the subject line has to count too.
    """
    gmail = FakeGmail([
        gmail_payload("Your application to Acme", "Thank you for applying",
                      "no-reply@greenhouse.io")
    ])
    report = GmailProcessor(session, gmail).process()
    assert report.matched == 1


def test_status_never_walks_backwards(session, application):
    """
    An automated "we received your application" arriving after an interview
    invitation must not undo the interview.
    """
    application.status = ApplicationStatus.INTERVIEW
    session.flush()

    gmail = FakeGmail([
        gmail_payload("Thank you for applying to Acme", "We received your application",
                      "noreply@acme.com")
    ])
    GmailProcessor(session, gmail).process()

    assert application.status is ApplicationStatus.INTERVIEW


def test_terminal_outcome_always_wins(session, application):
    application.status = ApplicationStatus.INTERVIEW
    session.flush()

    gmail = FakeGmail([
        gmail_payload("Update on your application",
                      "Unfortunately we will not be moving forward", "noreply@acme.com")
    ])
    GmailProcessor(session, gmail).process()

    assert application.status is ApplicationStatus.REJECTED


def test_unmatched_mail_is_reported_not_guessed(session, application):
    gmail = FakeGmail([
        gmail_payload("Interview invitation", "Let's schedule an interview",
                      "hr@totallydifferentcompany.com")
    ])
    report = GmailProcessor(session, gmail).process()

    assert report.matched == 0
    assert report.unmatched_senders


def test_gmail_failure_is_contained(session, application):
    class Broken:
        def list_messages(self, query, limit):
            raise RuntimeError("gmail unreachable")

        def get_message(self, message_id):
            raise RuntimeError

    report = GmailProcessor(session, Broken()).process()
    assert report.processed == 0


def test_html_only_mail_is_still_read(session, application):
    """A great many recruiter mails are HTML-only."""
    html = "<html><body><p>Let's schedule an interview</p></body></html>"
    payload = {
        "id": "1",
        "internalDate": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "talent@acme.com"},
                {"name": "Subject", "value": "Acme"},
            ],
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
                }
            ],
        },
    }
    report = GmailProcessor(session, FakeGmail([payload])).process()
    assert report.updated == 1
