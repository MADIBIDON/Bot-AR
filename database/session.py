"""Engine/session construction for real (non-test) usage.

Tests build their own isolated in-memory engine in tests/conftest.py and never
call into this module, so pytest can never touch the real database file.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from config.settings import get_database_url
from database.models import Base

_SQLITE_FILE_URL_RE = re.compile(r"^sqlite:///(?!:memory:)(.+)$")


def _ensure_sqlite_parent_dir(url: str) -> None:
    match = _SQLITE_FILE_URL_RE.match(url)
    if match is None:
        return
    Path(match.group(1)).parent.mkdir(parents=True, exist_ok=True)


def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_database_url()
    _ensure_sqlite_parent_dir(url)
    engine = create_engine(url)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(
            dbapi_connection: object, _connection_record: object
        ) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


_WATCH_RULE_OPPORTUNITY_COLUMNS = {
    "estimated_resale_price": "NUMERIC(10, 2)",
    "platform_fee_pct": "NUMERIC(5, 2)",
    "fixed_fee": "NUMERIC(10, 2)",
    "shipping_cost": "NUMERIC(10, 2)",
    "other_costs": "NUMERIC(10, 2)",
}


_WATCH_RULE_MARKET_DATA_COLUMNS = {
    "resale_price_mode": "TEXT NOT NULL DEFAULT 'manual'",
    "market_source": "TEXT",
}


def _ensure_watch_rule_opportunity_columns(engine: Engine) -> None:
    """Lightweight, idempotent patch for a SQLite database created before
    Phase 15 added these columns to WatchRule.

    Base.metadata.create_all() only creates missing *tables*, never adds
    columns to a table that already exists — so an existing real
    data/app.db needs these ALTER TABLE statements once. No Alembic: this
    is a local dev project on SQLite with a handful of nullable columns,
    a full migration framework would be overkill. A fresh database already
    has these columns from the model, so every check here is a no-op.
    """
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(watch_rules)")}
        for column, sql_type in _WATCH_RULE_OPPORTUNITY_COLUMNS.items():
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE watch_rules ADD COLUMN {column} {sql_type}")
        for column, sql_type in _WATCH_RULE_MARKET_DATA_COLUMNS.items():
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE watch_rules ADD COLUMN {column} {sql_type}")
        conn.commit()


def create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _ensure_watch_rule_opportunity_columns(engine)


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
