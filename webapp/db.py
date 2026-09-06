"""SQLModel engine + session management (design.md §4.1).

SQLite, single file at `webapp/data/platform.db` — one of the only two locations the platform is
allowed to write (CLAUDE.md §15). The engine is created lazily and can be swapped by tests
(`set_engine`) so the API test-suite runs against a throwaway temp DB without touching the real one.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlmodel import SQLModel, Session, create_engine

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_DB_PATH = DATA_DIR / "platform.db"

_engine = None


def _default_url() -> str:
    # env override lets tests / alternate deployments point elsewhere without code changes
    override = os.environ.get("TIMETABLE_DB_PATH")
    if override:
        return f"sqlite:///{override}"
    DATA_DIR.mkdir(parents=True, exist_ok=True)  # allowed write location (CLAUDE.md §15)
    return f"sqlite:///{DEFAULT_DB_PATH}"


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            _default_url(),
            echo=False,
            # FastAPI runs the sync solver in a threadpool -> connections cross threads
            connect_args={"check_same_thread": False},
        )
    return _engine


def set_engine(engine) -> None:
    """Override the process engine (used by tests to point at a temp SQLite file)."""
    global _engine
    _engine = engine


# Columns added to already-shipped tables after the first release. `create_all` only ever CREATEs
# missing tables -- it will not ALTER an existing one -- so a DB created before these columns
# existed keeps working but errors on every query that selects them. Each entry is applied as an
# additive `ALTER TABLE ... ADD COLUMN` (SQLite supports that form), which is safe to re-run because
# it is skipped when the column is already present, and lossless because it only ever adds.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "timetablerun": {
        "branch_ids": "JSON",
        "division_meta": "JSON",
    },
}


def _apply_additive_migrations(engine) -> list[str]:
    """Bring an existing SQLite file up to the current model definition. Returns the applied
    `table.column` names (empty on an already-current or freshly-created DB) so startup can log
    what changed instead of migrating silently."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    applied: list[str] = []

    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all just made it with every column already present
            present = {c["name"] for c in inspector.get_columns(table)}
            for column, sql_type in columns.items():
                if column in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
                applied.append(f"{table}.{column}")
    return applied


def init_db() -> list[str]:
    """Create all tables, then add any columns missing from a pre-existing DB. Import models first
    so they are registered on SQLModel.metadata. Returns the applied migrations."""
    import webapp.models_db  # noqa: F401  (registers table classes)
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    return _apply_additive_migrations(engine)


def get_session():
    """FastAPI dependency yielding a session bound to the current engine."""
    with Session(get_engine()) as session:
        yield session
