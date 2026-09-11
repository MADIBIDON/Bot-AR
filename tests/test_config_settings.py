from __future__ import annotations

import pytest

from config.settings import DEFAULT_UCP_AGENT_PROFILE_URL, get_ucp_agent_profile_url


def test_ucp_agent_profile_url_defaults_without_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PURCHASE_UCP_AGENT_PROFILE_URL", raising=False)

    assert get_ucp_agent_profile_url() == DEFAULT_UCP_AGENT_PROFILE_URL


def test_ucp_agent_profile_url_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PURCHASE_UCP_AGENT_PROFILE_URL", "https://staging.example/agent.json")

    assert get_ucp_agent_profile_url() == "https://staging.example/agent.json"


def test_ucp_agent_profile_url_blank_env_var_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PURCHASE_UCP_AGENT_PROFILE_URL", "   ")

    assert get_ucp_agent_profile_url() == DEFAULT_UCP_AGENT_PROFILE_URL


def test_default_url_is_https() -> None:
    assert DEFAULT_UCP_AGENT_PROFILE_URL.startswith("https://")
