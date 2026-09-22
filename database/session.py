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
    if url.startswith("sqlite") and ":memory:" not in url:
        # Phase 26 audit: the worker holds one long-lived connection while
        # the CLI (add-product/discover/edit-product/status/...) opens its
        # own, separate one against the same file — a real, observed
        # pattern this session (running `discover` while the worker was
        # mid-tick). SQLite's default journal mode locks the whole file
        # for the duration of a writer's transaction, and the default
        # busy_timeout is 0 — any other connection hitting that window
        # fails immediately with "database is locked" instead of waiting
        # a moment for the writer to finish. WAL lets readers proceed
        # without blocking on a writer at all (and vice versa) — the
        # right fit for "one writer process, occasional CLI reads/writes"
        # — with busy_timeout as the backstop for the genuinely-concurrent
        # writer-vs-writer case WAL doesn't eliminate. Skipped for
        # ":memory:" URLs (pytest's fixtures): an in-memory database has
        # no journal file, and WAL there is either a no-op or an error
        # depending on SQLAlchemy/pysqlite version.
        @event.listens_for(engine, "connect")
        def _configure_sqlite_connection(
            dbapi_connection: object, _connection_record: object
        ) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    elif url.startswith("sqlite"):

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


_PRODUCT_DISCOVERY_COLUMNS = {
    "discovery_interval": "INTEGER NOT NULL DEFAULT 1800",
    "last_discovery_at": "DATETIME",
}


def _ensure_product_discovery_columns(engine: Engine) -> None:
    """Idempotent patch for a SQLite database created before Phase 22
    added these columns to Product — same pattern/reasoning as
    _ensure_watch_rule_opportunity_columns() above."""
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(products)")}
        for column, sql_type in _PRODUCT_DISCOVERY_COLUMNS.items():
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE products ADD COLUMN {column} {sql_type}")
        conn.commit()


def _ensure_columns(engine: Engine, table: str, columns: dict[str, str]) -> None:
    """Generic version of the idempotent ALTER TABLE patches above —
    added in Phase 25 rather than converting the existing per-table
    functions, so their proven behavior stays untouched."""
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
        for column, sql_type in columns.items():
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        conn.commit()


# Phase 25 — profitability-based purchase decisions. Product gains the
# same Opportunity Engine config WatchRule already had (Phase 15/16), so
# it can be set once per Product Watch and shared by every auto-discovered
# WatchRule (engine.decision's effective_*() fallback functions read
# whichever level actually has a value). Both tables gain the same four
# new threshold columns.
_PRODUCT_OPPORTUNITY_COLUMNS = {
    "platform_fee_pct": "NUMERIC(5, 2)",
    "fixed_fee": "NUMERIC(10, 2)",
    "shipping_cost": "NUMERIC(10, 2)",
    "other_costs": "NUMERIC(10, 2)",
    "resale_price_mode": "TEXT NOT NULL DEFAULT 'manual'",
    "market_source": "TEXT",
}

_PROFITABILITY_THRESHOLD_COLUMNS = {
    "minimum_net_profit": "NUMERIC(10, 2)",
    "minimum_roi_pct": "NUMERIC(6, 2)",
    "minimum_resale_confidence": "TEXT",
    "estimated_resale_trusted": "BOOLEAN NOT NULL DEFAULT 0",
    "resale_updated_at": "DATETIME",  # Phase 33 section 21
}

# Phase 31 — Opportunity Intelligence. See WatchRule's own field comments
# in database/models.py for what each column is for.
_WATCH_RULE_ALERTING_COLUMNS = {
    "last_alert_tier": "TEXT",
    "scheduled_release_at": "DATETIME",
}

# Phase 36 — Cultura drop-window discovery. Mirrors
# _WATCH_RULE_ALERTING_COLUMNS's scheduled_release_at, one layer up (a
# Product with no Listing/WatchRule yet still needs a dynamic DISCOVERY
# cadence near a release — see app/discovery.py::is_discovery_due()).
_PRODUCT_RELEASE_COLUMNS = {"scheduled_release_at": "DATETIME"}

# Catalogue-watch relevance settings (app/relevance.py), added after the
# keyword_watches table already existed in real databases.
_KEYWORD_WATCH_RELEVANCE_COLUMNS = {
    "sealed_only": "BOOLEAN NOT NULL DEFAULT 1",
    "exclude_terms": "TEXT",
}

# Phase 33 — cross-listing purchase idempotency. See PurchaseAttempt's own
# docstring in database/models.py for why this closes a real gap (one
# active/purchased attempt per *product*, not just per listing).
_PURCHASE_ATTEMPT_PRODUCT_COLUMN = {"product_id": "INTEGER NOT NULL DEFAULT 0"}
_PURCHASE_ATTEMPT_PRODUCT_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_or_purchased_attempt_per_product "
    "ON purchase_attempts(product_id) "
    "WHERE status IN ('created', 'validating', 'checkout_started', 'purchased')"
)


def _ensure_purchase_attempt_product_index(engine: Engine) -> None:
    """Idempotent: CREATE UNIQUE INDEX IF NOT EXISTS is a no-op on a
    database that already has it (every fresh database, via
    Base.metadata.create_all; every restart after this phase first ran
    the migration below)."""
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.connect() as conn:
        conn.exec_driver_sql(_PURCHASE_ATTEMPT_PRODUCT_INDEX_SQL)
        conn.commit()


def create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _ensure_watch_rule_opportunity_columns(engine)
    _ensure_product_discovery_columns(engine)
    _ensure_columns(engine, "products", _PRODUCT_OPPORTUNITY_COLUMNS)
    _ensure_columns(engine, "products", _PROFITABILITY_THRESHOLD_COLUMNS)
    _ensure_columns(engine, "products", _PRODUCT_RELEASE_COLUMNS)
    _ensure_columns(engine, "keyword_watches", _KEYWORD_WATCH_RELEVANCE_COLUMNS)
    _ensure_columns(engine, "watch_rules", _PROFITABILITY_THRESHOLD_COLUMNS)
    _ensure_columns(engine, "watch_rules", _WATCH_RULE_ALERTING_COLUMNS)
    _ensure_columns(engine, "purchase_attempts", _PURCHASE_ATTEMPT_PRODUCT_COLUMN)
    _ensure_purchase_attempt_product_index(engine)


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
