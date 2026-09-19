"""
Shared Google OAuth handling for the Sheets and Gmail clients.

One place for the token dance, so the two integrations cannot drift into
different refresh behaviour — which is how the old code ended up with a stale
lock file it had to delete on every import.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def build_service(
    api: str,
    version: str,
    credentials_file: str | Path,
    token_file: str | Path,
    *,
    scopes: list[str],
) -> Any:
    """
    Build an authenticated Google API service.

    Imports googleapiclient lazily: most runs never touch Google, and making
    every one of them pay for the import (and the dependency) is not worth it.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials_file = Path(credentials_file)
    token_file = Path(token_file)

    if not credentials_file.is_file():
        raise FileNotFoundError(
            f"Google credentials not found at {credentials_file}. Download the "
            "OAuth client secret from Google Cloud Console and place it there."
        )

    creds = None
    if token_file.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes)
        except (ValueError, OSError) as exc:
            logger.warning("Stored Google token is unusable (%s); re-authenticating", exc)

    if creds is None or not creds.valid:
        refreshed = False
        if creds is not None and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError:
                # A revoked or expired refresh token is normal after a while;
                # fall through to a fresh consent rather than failing.
                logger.info("Google refresh token rejected; starting a new consent flow")
                creds = None

        if not refreshed:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes)
            creds = flow.run_local_server(port=0)

        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        # The token is a live credential; keep it off other users' hands where
        # the platform supports it.
        try:
            token_file.chmod(0o600)
        except OSError:
            pass

    return build(api, version, credentials=creds, cache_discovery=False)
