"""Purchase policy — the kill switch and every budget/allowlist knob,
loaded from environment variables only. Never logs or echoes a value
back (these aren't secrets, but the same discipline as
notifications/discord/config.py and market_data/ebay.py keeps one
pattern for "how config gets read" everywhere in this project).

PURCHASES_ENABLED defaults to False for anything except the exact
string "true" (case-insensitive) — unset, empty, "1", "yes", a typo, all
mean disabled. This is the kill switch: nothing downstream may treat an
ambiguous value as "on".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True, slots=True)
class PurchasePolicy:
    enabled: bool
    max_order_eur: Decimal | None
    max_daily_eur: Decimal | None
    allowed_merchant_domains: frozenset[str]
    cooldown_seconds: int


def _read_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value or None


def _parse_decimal_env(name: str) -> Decimal | None:
    raw = _read_env(name)
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{name} must be a valid decimal number, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be strictly positive, got {value}")
    return value


def _normalize_domain(domain: str) -> str:
    return domain.strip().lower().removeprefix("www.")


def _parse_allowed_merchants() -> frozenset[str]:
    raw = _read_env("PURCHASE_ALLOWED_MERCHANTS")
    if raw is None:
        return frozenset()
    return frozenset(_normalize_domain(item) for item in raw.split(",") if item.strip())


def _parse_cooldown_seconds() -> int:
    raw = _read_env("PURCHASE_COOLDOWN_SECONDS")
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"PURCHASE_COOLDOWN_SECONDS must be a whole number of seconds, got {raw!r}"
        ) from None
    if value < 0:
        raise ValueError(f"PURCHASE_COOLDOWN_SECONDS must not be negative, got {value}")
    return value


def load_purchase_policy() -> PurchasePolicy:
    enabled = (_read_env("PURCHASES_ENABLED") or "").lower() == "true"
    return PurchasePolicy(
        enabled=enabled,
        max_order_eur=_parse_decimal_env("PURCHASE_MAX_ORDER_EUR"),
        max_daily_eur=_parse_decimal_env("PURCHASE_MAX_DAILY_EUR"),
        allowed_merchant_domains=_parse_allowed_merchants(),
        cooldown_seconds=_parse_cooldown_seconds(),
    )


def is_merchant_allowed(policy: PurchasePolicy, merchant_domains: tuple[str, ...]) -> bool:
    """True if ANY of the merchant's known domains (connectors.defaults.
    MerchantDefinition.domains — a merchant can have more than one, e.g.
    with/without "www.") is in the allowlist. A merchant with no domains
    at all (shouldn't happen for a registered MerchantDefinition) is
    never allowed — fail closed."""
    normalized = {_normalize_domain(domain) for domain in merchant_domains}
    return bool(normalized & policy.allowed_merchant_domains)
