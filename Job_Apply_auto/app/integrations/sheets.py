"""
Google Sheets sync (section 16).

    Database = source of truth. Google Sheets = reporting layer.

That direction is absolute. Nothing is ever read back from the sheet into the
database: a user editing a cell must not silently change application state.

Rows are matched on a stable key and **updated in place**. The old tracker
appended blindly, which is how one job ends up as four rows after four retries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Application, Job

logger = logging.getLogger(__name__)

#: Column order, exactly as section 16 specifies it. The key column is first
#: so a human scanning the sheet can find a row, and it is hidden-ish at the
#: end of the visible set rather than interleaved with the data.
COLUMNS: tuple[str, ...] = (
    "Date",
    "Company",
    "Job Title",
    "Source",
    "Location",
    "Job URL",
    "Application URL",
    "Match Score",
    "Matching Skills",
    "Missing Skills",
    "Status",
    "Resume Version",
    "Cover Letter",
    "Applied Date",
    "Notes",
    "Error",
    "Last Updated",
    "Key",
)

KEY_COLUMN = "Key"


class SheetsClient(Protocol):
    """
    The slice of the Sheets API this module needs.

    Narrow on purpose: it keeps googleapiclient out of the import path for
    everyone who is not syncing, and it makes the sync testable with a fake.
    """

    def read_column(self, sheet_id: str, column: str) -> list[str]: ...
    def write_header(self, sheet_id: str, header: list[str]) -> None: ...
    def append_row(self, sheet_id: str, values: list[str]) -> None: ...
    def update_row(self, sheet_id: str, row_number: int, values: list[str]) -> None: ...


@dataclass(slots=True)
class SyncReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def summary(self) -> str:
        parts = [f"{self.created} added", f"{self.updated} updated"]
        if self.skipped:
            parts.append(f"{self.skipped} unchanged")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return ", ".join(parts)


def row_key(application: Application) -> str:
    """
    Stable identity for a row.

    Keyed on the application, not the job: a job legitimately has several
    attempts, and collapsing them would hide a retry history the user wants.
    """
    return f"app-{application.id}"


def _fmt_date(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def _fmt_list(values: list[str] | None, limit: int = 8) -> str:
    if not values:
        return ""
    shown = values[:limit]
    suffix = f" (+{len(values) - limit} more)" if len(values) > limit else ""
    return ", ".join(shown) + suffix


def build_row(application: Application, job: Job | None = None) -> list[str]:
    """Render one application as a sheet row, in COLUMNS order."""
    detail = (job.match_detail if job else None) or {}
    return [
        _fmt_date(application.created_at),
        application.company or "",
        application.job_title or "",
        application.source or "",
        (job.location if job else "") or "",
        application.job_url or "",
        application.application_url or "",
        f"{application.match_score:.2f}" if application.match_score is not None else "",
        _fmt_list(detail.get("matching_skills")),
        _fmt_list(detail.get("missing_skills")),
        str(application.status),
        application.resume_version or "",
        application.cover_letter_version or "",
        _fmt_date(application.submitted_at),
        detail.get("match_explanation", "")[:500],
        (application.error_message or "")[:500],
        _fmt_date(application.updated_at),
        row_key(application),
    ]


class SheetsSync:
    """
    Pushes applications into a tracking spreadsheet.

    Reads only the key column, then decides create-vs-update from it. Reading
    the whole sheet would be slower and would tempt the code into treating
    sheet contents as authoritative, which they are not.
    """

    def __init__(self, session: Session, client: SheetsClient, sheet_id: str) -> None:
        self.session = session
        self.client = client
        self.sheet_id = sheet_id

    def sync(self, applications: list[Application] | None = None) -> SyncReport:
        """
        Push applications to the sheet, updating existing rows in place.

        One failing row never stops the sync — a single malformed application
        should not block reporting on all the others.
        """
        report = SyncReport()

        if applications is None:
            applications = list(
                self.session.scalars(select(Application).order_by(Application.created_at))
            )

        try:
            self.client.write_header(self.sheet_id, list(COLUMNS))
            existing_keys = self.client.read_column(self.sheet_id, KEY_COLUMN)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not reach the tracking sheet")
            report.errors.append(f"Sheet unreachable: {exc}")
            return report

        # Row 1 is the header, so the first data row is row 2.
        key_to_row = {key: index + 2 for index, key in enumerate(existing_keys) if key}

        for application in applications:
            try:
                job = self.session.get(Job, application.job_id)
                values = build_row(application, job)
                key = row_key(application)

                if key in key_to_row:
                    self.client.update_row(self.sheet_id, key_to_row[key], values)
                    report.updated += 1
                else:
                    self.client.append_row(self.sheet_id, values)
                    report.created += 1
            except Exception as exc:  # noqa: BLE001 - isolate per row
                logger.warning("Failed to sync application %s: %s", application.id, exc)
                report.errors.append(f"application {application.id}: {exc}")

        return report


class GoogleSheetsClient:
    """
    Real Sheets client, built lazily.

    googleapiclient is imported inside the constructor so the rest of the
    platform does not depend on it — most runs never sync.
    """

    def __init__(self, credentials_file: str, token_file: str) -> None:
        from .google_auth import build_service

        self._service = build_service(
            "sheets", "v4", credentials_file, token_file,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )

    def _values(self) -> Any:
        return self._service.spreadsheets().values()

    def read_column(self, sheet_id: str, column: str) -> list[str]:
        try:
            index = COLUMNS.index(column)
        except ValueError:
            raise KeyError(f"Unknown column {column!r}") from None
        letter = _column_letter(index)
        result = self._values().get(
            spreadsheetId=sheet_id, range=f"{letter}2:{letter}"
        ).execute()
        return [row[0] if row else "" for row in result.get("values", [])]

    def write_header(self, sheet_id: str, header: list[str]) -> None:
        self._values().update(
            spreadsheetId=sheet_id,
            range=f"A1:{_column_letter(len(header) - 1)}1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()

    def append_row(self, sheet_id: str, values: list[str]) -> None:
        self._values().append(
            spreadsheetId=sheet_id,
            range="A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()

    def update_row(self, sheet_id: str, row_number: int, values: list[str]) -> None:
        self._values().update(
            spreadsheetId=sheet_id,
            range=f"A{row_number}:{_column_letter(len(values) - 1)}{row_number}",
            valueInputOption="RAW",
            body={"values": [values]},
        ).execute()


def _column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
