from __future__ import annotations

from decimal import Decimal

from connectors.base import ConnectorProduct
from database.models import Product
from discovery.matcher import classify_candidate


def _product(**overrides: object) -> Product:
    defaults: dict[str, object] = dict(
        id=1, name="ETB Pokemon Chaos Ascendant FR", ean=None, mpn=None
    )
    defaults.update(overrides)
    return Product(**defaults)  # type: ignore[arg-type]


def _candidate(**overrides: object) -> ConnectorProduct:
    defaults: dict[str, object] = dict(
        external_id="etb-chaos-ascendant-fr",
        name="ETB Pokemon Chaos Ascendant FR",
        price=Decimal("59.90"),
        currency="EUR",
        available=True,
        seller="Kairyu",
        url="https://kairyu.fr/products/etb-chaos-ascendant-fr",
        ean=None,
        mpn=None,
    )
    defaults.update(overrides)
    return ConnectorProduct(**defaults)  # type: ignore[arg-type]


def test_ean_exact_match_auto_links() -> None:
    product = _product(ean="1234567890123")
    candidate = _candidate(ean="1234567890123")

    match = classify_candidate(product, candidate)

    assert match.verdict == "auto_link"
    assert match.confidence == 100


def test_ean_mismatch_is_no_match_even_if_names_agree() -> None:
    product = _product(ean="1234567890123")
    candidate = _candidate(ean="9999999999999")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_mpn_exact_match_auto_links() -> None:
    product = _product(mpn="POKETBME04-FR")
    candidate = _candidate(mpn="POKETBME04-FR")

    match = classify_candidate(product, candidate)

    assert match.verdict == "auto_link"
    assert match.confidence == 90


def test_mpn_mismatch_is_no_match() -> None:
    product = _product(mpn="POKETBME04-FR")
    candidate = _candidate(mpn="POKETBME05-FR")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_exact_normalized_name_with_no_identifiers_auto_links() -> None:
    product = _product(name="ETB Pokemon Chaos Ascendant FR")
    candidate = _candidate(name="ETB Pokemon Chaos Ascendant FR!")  # punctuation-only diff

    match = classify_candidate(product, candidate)

    assert match.verdict == "auto_link"
    assert match.confidence == 75


def test_similar_but_not_exact_name_is_candidate_only() -> None:
    product = _product(name="ETB Pokemon Chaos Ascendant FR")
    candidate = _candidate(name="Elite Trainer Box Chaos Ascendant Pokemon France")

    match = classify_candidate(product, candidate)

    assert match.verdict == "candidate"
    assert match.confidence == 50


def test_language_mismatch_is_no_match() -> None:
    product = _product(name="ETB Pokemon Chaos Ascendant FR")
    candidate = _candidate(name="ETB Pokemon Chaos Ascendant JP")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_product_type_mismatch_is_no_match() -> None:
    product = _product(name="ETB Pokemon Chaos Ascendant FR")
    candidate = _candidate(name="Display Pokemon Chaos Ascendant FR")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_unrelated_product_is_no_match() -> None:
    product = _product(name="ETB Pokemon Chaos Ascendant FR")
    candidate = _candidate(name="Yu-Gi-Oh Structure Deck Random")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"
