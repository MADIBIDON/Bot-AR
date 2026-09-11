"""Centralized configuration. No secrets or values are hardcoded here —
the one exception is DEFAULT_UCP_AGENT_PROFILE_URL below, which is a
public URL (this project's own hosted UCP agent profile document, not a
credential) rather than a secret.
"""

from __future__ import annotations

import os

DEFAULT_DATABASE_URL = "sqlite:///./data/app.db"


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


# Phase 23: single source of truth for this project's own UCP agent
# profile URL (see docs/UCP_HOSTING.md / deploy/ucp-profile/). Previously
# every call site fell back to "" when PURCHASE_UCP_AGENT_PROFILE_URL
# wasn't set, silently disabling Kairyu/RelicTCG's UCP discovery and
# purchase connectors unless someone remembered to export it by hand each
# run — a real, live-hit gap (the actual .env never had it). This is not
# a secret: it's a public, unauthenticated document this project hosts
# itself, so a hardcoded default is safe and correct. Still fully
# overridable via the env var, e.g. to point at a staging profile.
DEFAULT_UCP_AGENT_PROFILE_URL = "https://ucp-profile.vercel.app/agent-profile.json"


def get_ucp_agent_profile_url() -> str:
    return os.environ.get("PURCHASE_UCP_AGENT_PROFILE_URL", "").strip() or (
        DEFAULT_UCP_AGENT_PROFILE_URL
    )
