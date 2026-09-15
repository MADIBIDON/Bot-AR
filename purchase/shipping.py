"""Local checkout profile — Phase 40.

Real personal data (name/address/phone) for the one person this project
buys on behalf of, read from environment variables only. Matches
purchase/config.py's own discipline: never hardcoded, never logged, never
echoed back in an exception message, never present in a test (every test
that exercises this reads from monkeypatch.setenv with fake values).

Used by a connector to advance its own checkout state machine (shipping
address, billing address) — never to fill in a payment field.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShippingAddress:
    firstname: str
    lastname: str
    street: str
    city: str
    postal_code: str
    country_code: str
    telephone: str


_REQUIRED_ENV_VARS = (
    "PURCHASE_SHIPPING_FIRST_NAME",
    "PURCHASE_SHIPPING_LAST_NAME",
    "PURCHASE_SHIPPING_ADDRESS",
    "PURCHASE_SHIPPING_CITY",
    "PURCHASE_SHIPPING_POSTAL_CODE",
    "PURCHASE_SHIPPING_COUNTRY",
)


def _read_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value or None


def load_shipping_address() -> ShippingAddress | None:
    """None (never a partially-filled address) unless every field in
    _REQUIRED_ENV_VARS is set. Telephone is optional — some merchants'
    address forms don't require it."""
    values = {name: _read_env(name) for name in _REQUIRED_ENV_VARS}
    if not all(values.values()):
        return None
    return ShippingAddress(
        firstname=values["PURCHASE_SHIPPING_FIRST_NAME"],  # type: ignore[arg-type]
        lastname=values["PURCHASE_SHIPPING_LAST_NAME"],  # type: ignore[arg-type]
        street=values["PURCHASE_SHIPPING_ADDRESS"],  # type: ignore[arg-type]
        city=values["PURCHASE_SHIPPING_CITY"],  # type: ignore[arg-type]
        postal_code=values["PURCHASE_SHIPPING_POSTAL_CODE"],  # type: ignore[arg-type]
        country_code=values["PURCHASE_SHIPPING_COUNTRY"],  # type: ignore[arg-type]
        telephone=_read_env("PURCHASE_CONTACT_PHONE") or "",
    )
