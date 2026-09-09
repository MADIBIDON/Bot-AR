"""Centralized configuration. No secrets or values are hardcoded here."""

from __future__ import annotations

import os

DEFAULT_DATABASE_URL = "sqlite:///./data/app.db"


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
