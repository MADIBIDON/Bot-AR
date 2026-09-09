from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from connectors.base import ConnectorProduct
from products.observation import ProductObservation


def _connector_product(**overrides: object) -> ConnectorProduct:
    defaults: dict[str, object] = dict(
        external_id="fake-123",
        name="Duopack Evoli 30 ans",
        price=Decimal("13.99"),
        currency="EUR",
        available=True,
        seller="FakeStore",
        url="https://fake-store.example/p/fake-123",
        ean="1234567890123",
        mpn=None,
    )
    defaults.update(overrides)
    return ConnectorProduct(**defaults)  # type: ignore[arg-type]


def _observation(**overrides: object) -> ProductObservation:
    defaults: dict[str, object] = dict(
        merchant="FakeStore",
        external_id="fake-123",
        name="Duopack Evoli 30 ans",
        price=Decimal("13.99"),
        currency="EUR",
        available=True,
        url="https://fake-store.example/p/fake-123",
        observed_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return ProductObservation(**defaults)  # type: ignore[arg-type]


def test_from_connector_product_maps_fields() -> None:
    product = _connector_product()

    observation = ProductObservation.from_connector_product(product, merchant="FakeStore")

    assert observation.merchant == "FakeStore"
    assert observation.external_id == "fake-123"
    assert observation.name == "Duopack Evoli 30 ans"
    assert observation.ean == "1234567890123"
    assert observation.mpn is None
    assert observation.seller == "FakeStore"
    assert observation.url == "https://fake-store.example/p/fake-123"
    assert observation.available is True


def test_from_connector_product_preserves_mpn_when_present() -> None:
    product = _connector_product(mpn="MPN-001", ean=None)

    observation = ProductObservation.from_connector_product(product, merchant="FakeStore")

    assert observation.mpn == "MPN-001"
    assert observation.ean is None


def test_observed_at_defaults_to_now_utc() -> None:
    product = _connector_product()
    before = datetime.now(UTC)

    observation = ProductObservation.from_connector_product(product, merchant="FakeStore")

    after = datetime.now(UTC)
    assert observation.observed_at.tzinfo is not None
    assert observation.observed_at.utcoffset() == timedelta(0)
    assert before <= observation.observed_at <= after


def test_observed_at_non_utc_input_is_normalized_to_utc() -> None:
    product = _connector_product()
    paris_summer_time = timezone(timedelta(hours=2))
    observed_at = datetime(2026, 6, 1, 14, 0, tzinfo=paris_summer_time)

    observation = ProductObservation.from_connector_product(
        product, merchant="FakeStore", observed_at=observed_at
    )

    assert observation.observed_at.utcoffset() == timedelta(0)
    assert observation.observed_at == observed_at.astimezone(UTC)


def test_naive_observed_at_is_rejected() -> None:
    with pytest.raises(ValueError, match="observed_at"):
        _observation(observed_at=datetime(2026, 6, 1, 14, 0))


def test_price_precision_is_preserved() -> None:
    product = _connector_product(price=Decimal("19.99"))

    observation = ProductObservation.from_connector_product(product, merchant="FakeStore")

    assert observation.price == Decimal("19.99")


def test_negative_price_is_rejected() -> None:
    with pytest.raises(ValueError, match="price"):
        _observation(price=Decimal("-1"))


def test_zero_price_is_accepted() -> None:
    observation = _observation(price=Decimal("0"))
    assert observation.price == Decimal("0")


def test_empty_external_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="external_id"):
        _observation(external_id="   ")


def test_empty_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="name"):
        _observation(name="")


def test_empty_merchant_is_rejected() -> None:
    with pytest.raises(ValueError, match="merchant"):
        _observation(merchant="")


def test_empty_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="url"):
        _observation(url="")


def test_currency_is_normalized_to_uppercase() -> None:
    observation = _observation(currency="eur")
    assert observation.currency == "EUR"


def test_empty_currency_is_rejected() -> None:
    with pytest.raises(ValueError, match="currency"):
        _observation(currency="   ")


def test_missing_ean_and_mpn_are_accepted() -> None:
    observation = _observation()
    assert observation.ean is None
    assert observation.mpn is None


def test_missing_seller_is_accepted() -> None:
    observation = _observation()
    assert observation.seller is None
