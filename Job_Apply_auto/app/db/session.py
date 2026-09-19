"""
Database engine and session management.

SQLite by default, Postgres by setting DATABASE_URL. The differences between
the two that actually matter here (foreign-key enforcement, concurrent
readers) are handled at connect time so callers never branch on backend.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings, get_settings
from ..models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    """
    Apply the SQLite pragmas this workload needs.

    - foreign_keys: OFF by default in SQLite, which would silently ignore
      every ondelete rule in the models.
    - WAL: the scheduler writes while the API reads; without it readers block
      on any open write transaction.
    - busy_timeout: makes a concurrent write wait rather than raise
      "database is locked" immediately.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def get_engine(settings: Settings | None = None) -> Engine:
    """The process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        url = settings.database_url
        if url.startswith("sqlite"):
            # Ensure the directory exists — SQLite will not create it.
            db_file = url.split("///", 1)[-1]
            if db_file and db_file != ":memory:":
                Path(db_file).parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(url, future=True, echo=False)
            _configure_sqlite(_engine)
        else:
            _engine = create_engine(url, future=True, echo=False, pool_pre_ping=True)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(settings), expire_on_commit=False, future=True
        )
    return _SessionFactory


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """
    Transactional scope. Commits on success, rolls back on any exception.

    Used by the worker loops, where a half-applied state change after a crash
    is worse than losing the step entirely — the job stays in its previous
    status and gets picked up again.
    """
    session = get_session_factory(settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def init_db(settings: Settings | None = None) -> None:
    """
    Create tables that do not exist yet.

    Alembic owns schema *changes*; this is for a first run and for tests. It
    never drops or alters anything.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()
    Base.metadata.create_all(get_engine(settings))


def reset_engine() -> None:
    """Drop cached engine/factory. Tests use this to rebind to a temp DB."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
