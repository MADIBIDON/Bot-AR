"""Engine/session construction for real (non-test) usage.

Tests build their own isolated in-memory engine in tests/conftest.py and never
call into this module, so pytest can never touch the real database file.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import get_database_url
from database.models import Base


def get_engine(database_url: str | None = None) -> Engine:
    return create_engine(database_url or get_database_url())


def create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
