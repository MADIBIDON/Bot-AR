from __future__ import annotations

from decimal import Decimal

import pytest

from purchase.config import is_merchant_allowed, load_purchase_policy


def _clear_purchase_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PURCHASES_ENABLED",
        "PURCHASE_MAX_ORDER_EUR",
        "PURCHASE_MAX_DAILY_EUR",
        "PURCHASE_ALLOWED_MERCHANTS",
        "PURCHASE_COOLDOWN_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_to_disabled_with_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_purchase_env(monkeypatch)

    policy = load_purchase_policy()

    assert policy.enabled is False
    assert policy.max_order_eur is None
    assert policy.max_daily_eur is None
    assert policy.allowed_merchant_domains == frozenset()
    assert policy.cooldown_seconds == 0


@pytest.mark.parametrize("value", ["True", "TRUE", " true ", "true"])
def test_enabled_accepts_only_exact_true_case_insensitive(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_purchase_env(monkeypatch)
    monkeypatch.setenv("PURCHASES_ENABLED", value)

    assert load_purchase_policy().enabled is True


@pytest.mark.parametrize("value", ["1", "yes", "on", "enabled", "", "false", "no"])
def test_enabled_rejects_anything_but_true(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_purchase_env(monkeypatch)
    monkeypatch.setenv("PURCHASES_ENABLED", value)

    assert load_purchase_policy().enabled is False


def test_parses_budgets_and_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_purchase_env(monkeypatch)
    monkeypatch.setenv("PURCHASE_MAX_ORDER_EUR", "100")
    monkeypatch.setenv("PURCHASE_MAX_DAILY_EUR", "300")
    monkeypatch.setenv("PURCHASE_COOLDOWN_SECONDS", "300")

    policy = load_purchase_policy()

    assert policy.max_order_eur == Decimal("100")
    assert policy.max_daily_eur == Decimal("300")
    assert policy.cooldown_seconds == 300


def test_rejects_zero_or_negative_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_purchase_env(monkeypatch)
    monkeypatch.setenv("PURCHASE_MAX_ORDER_EUR", "0")

    with pytest.raises(ValueError, match="strictly positive"):
        load_purchase_policy()


def test_rejects_negative_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_purchase_env(monkeypatch)
    monkeypatch.setenv("PURCHASE_COOLDOWN_SECONDS", "-1")

    with pytest.raises(ValueError, match="must not be negative"):
        load_purchase_policy()


def test_parses_allowed_merchants_normalizing_www_and_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_purchase_env(monkeypatch)
    monkeypatch.setenv("PURCHASE_ALLOWED_MERCHANTS", "Kairyu.fr, www.RelicTCG.com")

    policy = load_purchase_policy()

    assert policy.allowed_merchant_domains == frozenset({"kairyu.fr", "relictcg.com"})


def test_is_merchant_allowed_matches_any_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_purchase_env(monkeypatch)
    monkeypatch.setenv("PURCHASE_ALLOWED_MERCHANTS", "kairyu.fr")
    policy = load_purchase_policy()

    assert is_merchant_allowed(policy, ("kairyu.fr", "www.kairyu.fr")) is True
    assert is_merchant_allowed(policy, ("relictcg.com",)) is False


def test_empty_allowlist_allows_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_purchase_env(monkeypatch)
    policy = load_purchase_policy()

    assert is_merchant_allowed(policy, ("kairyu.fr",)) is False
