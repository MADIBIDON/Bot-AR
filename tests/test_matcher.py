from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from database.models import Listing, Merchant, Product
from products.matcher import MatchResult, match_product
from products.observation import ProductObservation


def _product(**overrides: object) -> Product:
    defaults: dict[str, object] = dict(name="Duopack Evoli 30 ans", ean=None, gtin=None, mpn=None)
    defaults.update(overrides)
    return Product(**defaults)  # type: ignore[arg-type]


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


def test_ean_exact_match() -> None:
    product = _product(ean="1234567890123")
    observation = _observation(ean="1234567890123")

    result = match_product(product, observation)

    assert result.matched is True
    assert result.confidence == 100
    assert result.method == "ean_exact"


def test_ean_mismatch_is_hard_reject() -> None:
    product = _product(ean="1234567890123")
    observation = _observation(ean="9999999999999")

    result = match_product(product, observation)

    assert result.matched is False
    assert result.confidence == 0
    assert result.method == "ean_mismatch"


def test_ean_absent_on_one_side_falls_through_to_name() -> None:
    product = _product(ean="1234567890123", name="Duopack Evoli 30 ans")
    observation = _observation(ean=None, name="Duopack Evoli 30 ans")

    result = match_product(product, observation)

    assert result.method == "name_exact_normalized"
    assert result.confidence == 50


def test_mpn_exact_without_ean() -> None:
    product = _product(mpn="mpn-001")
    observation = _observation(mpn="MPN-001")

    result = match_product(product, observation)

    assert result.matched is True
    assert result.confidence == 90
    assert result.method == "mpn_exact"


def test_mpn_mismatch_is_hard_reject() -> None:
    product = _product(mpn="MPN-001")
    observation = _observation(mpn="MPN-002")

    result = match_product(product, observation)

    assert result.matched is False
    assert result.confidence == 0
    assert result.method == "mpn_mismatch"


def test_identical_name_without_strong_identifier() -> None:
    product = _product(name="Duopack Evoli 30 ans")
    observation = _observation(name="Duopack Evoli 30 ans")

    result = match_product(product, observation)

    assert result.matched is True
    assert result.method == "name_exact_normalized"
    assert result.confidence == 50


def test_name_only_match_never_reaches_full_confidence() -> None:
    product = _product(name="Duopack Evoli 30 ans")
    observation = _observation(name="Duopack Evoli 30 ans")

    result = match_product(product, observation)

    assert result.confidence < 100


def test_different_names_no_identifier_no_match() -> None:
    product = _product(name="Duopack Evoli 30 ans")
    observation = _observation(name="Console de jeu retro portable")

    result = match_product(product, observation)

    assert result.matched is False
    assert result.method == "no_match"
    assert result.confidence == 0


def test_name_case_and_whitespace_are_normalized() -> None:
    product = _product(name="  Duopack   ÉVOLI 30 Ans ")
    observation = _observation(name="duopack évoli 30 ans")

    result = match_product(product, observation)

    assert result.matched is True
    assert result.method == "name_exact_normalized"


def test_insufficient_data_when_product_name_is_blank() -> None:
    product = _product(name="   ")
    observation = _observation(name="Duopack Evoli 30 ans")

    result = match_product(product, observation)

    assert result.matched is False
    assert result.confidence == 0
    assert result.method == "insufficient_data"


def test_ean_exact_even_with_slightly_different_name() -> None:
    product = _product(ean="1234567890123", name="Duopack Evoli 30 ans")
    observation = _observation(ean="1234567890123", name="Pack Evoli anniversaire")

    result = match_product(product, observation)

    assert result.matched is True
    assert result.confidence == 100
    assert result.method == "ean_exact"


def test_ean_mismatch_even_with_identical_name() -> None:
    product = _product(ean="1234567890123", name="Duopack Evoli 30 ans")
    observation = _observation(ean="9999999999999", name="Duopack Evoli 30 ans")

    result = match_product(product, observation)

    assert result.matched is False
    assert result.confidence == 0
    assert result.method == "ean_mismatch"


def _listing(merchant_name: str, external_id: str | None) -> Listing:
    merchant = Merchant(name=merchant_name)
    return Listing(merchant=merchant, external_id=external_id, url="https://a.example/p/1")


def test_expected_listing_with_matching_external_id() -> None:
    product = _product(name="Duopack Evoli 30 ans")
    observation = _observation(
        merchant="RetailerA", external_id="SKU-123", name="Autre libelle produit"
    )
    listing = _listing("RetailerA", "SKU-123")

    result = match_product(product, observation, expected_listing=listing)

    assert result.matched is True
    assert result.confidence == 80
    assert result.method == "external_id_exact"


def test_expected_listing_with_wrong_external_id() -> None:
    product = _product(name="Duopack Evoli 30 ans")
    observation = _observation(merchant="RetailerA", external_id="SKU-999")
    listing = _listing("RetailerA", "SKU-123")

    result = match_product(product, observation, expected_listing=listing)

    assert result.matched is False
    assert result.confidence == 0
    assert result.method == "external_id_mismatch"


def test_expected_listing_with_wrong_merchant() -> None:
    product = _product(name="Duopack Evoli 30 ans")
    observation = _observation(merchant="RetailerB", external_id="SKU-123")
    listing = _listing("RetailerA", "SKU-123")

    result = match_product(product, observation, expected_listing=listing)

    assert result.matched is False
    assert result.confidence == 0
    assert result.method == "merchant_mismatch"


def test_expected_listing_without_external_id_falls_through_to_name() -> None:
    product = _product(name="Duopack Evoli 30 ans")
    observation = _observation(merchant="RetailerA", name="Duopack Evoli 30 ans")
    listing = _listing("RetailerA", None)

    result = match_product(product, observation, expected_listing=listing)

    assert result.method == "name_exact_normalized"


def test_match_result_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError, match="confidence"):
        MatchResult(matched=True, confidence=101, method="x", reason="x")
