"""Isolated in-memory SQLite fixtures — tests never touch the real database."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    db_session = session_factory()
    try:
        yield db_session
    finally:
        db_session.close()


@pytest.fixture(autouse=True)
def _reset_alert_cooldown():
    """notifications/dedup.py keeps process-local state so a flapping
    stock does not produce the same alert five times. That state must
    never leak between tests — otherwise the first test to send an
    alert silences every later one that happens to use the same rule,
    event and price."""
    from notifications.dedup import get_default_cooldown

    get_default_cooldown()._last_sent.clear()
    yield
    get_default_cooldown()._last_sent.clear()
