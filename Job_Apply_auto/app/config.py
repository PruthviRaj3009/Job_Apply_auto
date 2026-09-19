"""
Single source of configuration for the platform.

Replaces the three competing schemes found in the audit (JobPilot's `.env` +
`config/*.py` module constants, and job-apply-mcp's `~/.job-apply-mcp/
config.json`). Everything is env-driven with explicit defaults, so the API,
CLI, MCP server and scheduler cannot drift apart the way the old `:8000` vs
`:8080` split did.

Secrets are never written to disk by this module. Portal credentials are read
from the environment only; the old config.json stored passwords in cleartext.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # ---------------------------------------------------------------- paths
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    log_dir: Path = Field(default=PROJECT_ROOT / ".logs")

    # ------------------------------------------------------------- database
    #: SQLAlchemy URL. SQLite by default; set DATABASE_URL to a postgresql://
    #: DSN for a shared deployment — no code changes needed.
    database_url: str = Field(default="")

    # ------------------------------------------------------------------ api
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    #: Browser origins allowed to call the API. The old service used "*" with
    #: credentials enabled, which lets any site issue authenticated requests.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    #: Shared secret for the API. Empty disables auth, which is only safe for
    #: a purely local bind — `require_api_key` enforces that.
    api_key: str = ""

    # ------------------------------------------------------------ behaviour
    #: Master switch. While true, nothing is ever submitted to a real portal.
    dry_run: bool = True
    #: Must be explicitly enabled before any application is sent (section 14).
    auto_apply: bool = False
    #: Below this score a job is rejected rather than applied to.
    min_match_score: float = 0.70
    #: Applications to one company inside `company_window_days`.
    max_applications_per_company: int = 2
    company_window_days: int = 1
    max_applications_per_run: int = 10

    # ------------------------------------------------------------------ llm
    llm_model: str = "phi3"
    embed_model: str = "mxbai-embed-large"
    embed_collection: str = "jobpilot_context"
    chroma_dir: Path = Field(default=PROJECT_ROOT / ".chroma_db")
    #: Minimum semantic similarity for reusing a stored answer. Below this the
    #: application pauses and asks the user (section 11).
    answer_confidence_threshold: float = 0.82

    # -------------------------------------------------------------- browser
    browser: str = "firefox"
    headless: bool = False
    browser_profile_dir: Path = Field(default=PROJECT_ROOT / "data" / "browser-profiles")
    page_timeout_ms: int = 30_000
    #: Politeness delay between requests to the same source.
    request_delay_seconds: float = 3.0

    # ---------------------------------------------------------------- latex
    latex_command: str = "pdflatex"
    resume_output_dir: Path = Field(default=PROJECT_ROOT / "data" / "generated")

    # --------------------------------------------------------------- google
    gmail_credentials_file: Path = Field(default=PROJECT_ROOT / ".credentials" / "client_secret.json")
    gmail_token_file: Path = Field(default=PROJECT_ROOT / ".credentials" / "token.json")
    google_sheet_id: str = ""

    # -------------------------------------------------------------- logging
    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept a comma-separated string from the environment."""
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("browser")
    @classmethod
    def _known_browser(cls, v: str) -> str:
        allowed = {"firefox", "chromium", "webkit"}
        if v not in allowed:
            raise ValueError(f"browser must be one of {sorted(allowed)}, got {v!r}")
        return v

    def model_post_init(self, _context: object) -> None:
        # Default the DB to a file under data_dir, which the user may have
        # relocated — so this cannot be a plain field default.
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'jobpilot.db'}"

    @property
    def is_local_only(self) -> bool:
        return self.api_host in {"127.0.0.1", "localhost", "::1"}

    @property
    def require_api_key(self) -> bool:
        """
        Whether requests must carry an API key.

        Binding to a non-loopback interface without a key would expose an
        unauthenticated service that can spend money and act as the user, so
        that combination is rejected at startup rather than silently allowed.
        """
        if self.is_local_only:
            return bool(self.api_key)
        if not self.api_key:
            raise RuntimeError(
                f"API_HOST is {self.api_host!r} (not loopback) but API_KEY is unset. "
                "Set API_KEY, or bind to 127.0.0.1."
            )
        return True

    def portal_credentials(self, platform: str) -> tuple[str | None, str | None]:
        """
        Credentials for a portal, from the environment only.

        e.g. LINKEDIN_EMAIL / LINKEDIN_PASSWORD. Returning (None, None) is
        normal and means "use the saved browser session instead" — most
        portals are better driven by an interactive login than by replaying a
        stored password.
        """
        key = platform.upper()
        return os.getenv(f"{key}_EMAIL"), os.getenv(f"{key}_PASSWORD")

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.log_dir,
            self.chroma_dir,
            self.resume_output_dir,
            self.browser_profile_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so every caller sees the same object."""
    return Settings()
