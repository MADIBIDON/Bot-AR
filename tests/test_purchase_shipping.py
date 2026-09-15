"""purchase/shipping.py. Every value used here is synthetic/fake — never
the real local profile in .env — matching this project's standing rule
that real personal data never appears in a test.
"""

from __future__ import annotations

import pytest

from purchase.shipping import ShippingAddress, load_shipping_address

_FAKE_VARS = {
    "PURCHASE_SHIPPING_FIRST_NAME": "Test",
    "PURCHASE_SHIPPING_LAST_NAME": "User",
    "PURCHASE_SHIPPING_ADDRESS": "1 rue de Test",
    "PURCHASE_SHIPPING_CITY": "Testville",
    "PURCHASE_SHIPPING_POSTAL_CODE": "00000",
    "PURCHASE_SHIPPING_COUNTRY": "FR",
}


def _clear_all(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_FAKE_VARS, "PURCHASE_CONTACT_PHONE"):
        monkeypatch.delenv(name, raising=False)


def test_returns_none_when_nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all(monkeypatch)
    assert load_shipping_address() is None


def test_returns_none_when_partially_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all(monkeypatch)
    monkeypatch.setenv("PURCHASE_SHIPPING_FIRST_NAME", "Test")
    monkeypatch.setenv("PURCHASE_SHIPPING_LAST_NAME", "User")
    # city/address/postal_code/country missing — must never return a
    # half-filled address.
    assert load_shipping_address() is None


def test_returns_full_address_when_all_required_fields_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all(monkeypatch)
    for name, value in _FAKE_VARS.items():
        monkeypatch.setenv(name, value)

    address = load_shipping_address()

    assert address == ShippingAddress(
        firstname="Test",
        lastname="User",
        street="1 rue de Test",
        city="Testville",
        postal_code="00000",
        country_code="FR",
        telephone="",
    )


def test_telephone_is_optional_but_used_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all(monkeypatch)
    for name, value in _FAKE_VARS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PURCHASE_CONTACT_PHONE", "+33000000000")

    address = load_shipping_address()

    assert address is not None
    assert address.telephone == "+33000000000"


def test_strips_surrounding_quotes_like_purchase_config_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all(monkeypatch)
    for name, value in _FAKE_VARS.items():
        monkeypatch.setenv(name, f'"{value}"')

    address = load_shipping_address()

    assert address is not None
    assert address.firstname == "Test"
    assert address.city == "Testville"
