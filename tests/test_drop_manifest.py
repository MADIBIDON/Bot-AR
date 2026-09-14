"""app/drop_manifest.py — Phase 33 section 6. No real network: URL-based
entries monkeypatch connectors.defaults.find_merchant_for_domain with a
fake MerchantDefinition/connector, same style as other connector tests
in this project.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.drop_manifest import (
    DropManifestEntry,
    DropManifestError,
    import_drop_manifest,
    load_drop_manifest,
)
from connectors.base import ConnectorError, ConnectorProduct
from database import crud


def test_missing_canonical_name_is_rejected() -> None:
    with pytest.raises(DropManifestError):
        DropManifestEntry.from_dict({"ean": "123"})


def test_product_url_without_merchant_is_rejected() -> None:
    with pytest.raises(DropManifestError):
        DropManifestEntry.from_dict(
            {"canonical_name": "ETB 30e", "product_url": "https://kairyu.fr/products/etb"}
        )


def test_unsupported_max_successful_purchases_is_rejected() -> None:
    with pytest.raises(DropManifestError):
        DropManifestEntry.from_dict({"canonical_name": "ETB 30e", "max_successful_purchases": 3})


def test_invalid_release_at_is_rejected() -> None:
    with pytest.raises(DropManifestError):
        DropManifestEntry.from_dict({"canonical_name": "ETB 30e", "release_at": "not-a-date"})


def test_load_drop_manifest_requires_a_top_level_array(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"canonical_name": "ETB 30e"}), encoding="utf-8")

    with pytest.raises(DropManifestError):
        load_drop_manifest(path)


def test_load_drop_manifest_parses_a_real_shaped_file(tmp_path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            [
                {
                    "canonical_name": "ETB Pokémon 30e Anniversaire",
                    "ean": "0196214142145",
                    "release_at": "2026-09-16T08:00:00Z",
                    "target_price": 59.99,
                    "quantity": 1,
                    "purchase_allowed": False,
                }
            ]
        ),
        encoding="utf-8",
    )

    entries = load_drop_manifest(path)

    assert len(entries) == 1
    assert entries[0].canonical_name == "ETB Pokémon 30e Anniversaire"
    assert entries[0].ean == "0196214142145"
    assert entries[0].release_at is not None


def test_monitoring_only_entry_creates_no_purchase_thresholds(session: Session) -> None:
    entry = DropManifestEntry.from_dict(
        {"canonical_name": "ETB 30e Anniversaire", "ean": "111", "target_price": 59.99}
    )

    results = import_drop_manifest(session, [entry])

    assert len(results) == 1
    assert results[0].status == "created"
    rule = crud.get_watch_rule(session, results[0].watch_rule_id)
    assert rule.target_price == Decimal("59.99")
    assert rule.max_price is None  # purchase_allowed defaults False -> no acquisition ceiling
    assert rule.listing_id is None  # no product_url given


def test_purchase_allowed_entry_sets_max_price(session: Session) -> None:
    entry = DropManifestEntry.from_dict(
        {
            "canonical_name": "ETB 30e Anniversaire",
            "ean": "222",
            "purchase_allowed": True,
            "max_acquisition_total": 70,
            "minimum_net_profit": 20,
            "minimum_roi_pct": 30,
        }
    )

    results = import_drop_manifest(session, [entry])

    rule = crud.get_watch_rule(session, results[0].watch_rule_id)
    assert rule.max_price == Decimal("70")
    assert rule.minimum_net_profit == Decimal("20")
    assert rule.minimum_roi_pct == Decimal("30")


def test_reimporting_the_same_ean_reuses_the_product_and_rule(session: Session) -> None:
    entry = DropManifestEntry.from_dict({"canonical_name": "ETB 30e Anniversaire", "ean": "333"})

    first = import_drop_manifest(session, [entry])
    second = import_drop_manifest(session, [entry])

    assert first[0].status == "created"
    assert second[0].status == "reused"
    assert first[0].product_id == second[0].product_id
    assert first[0].watch_rule_id == second[0].watch_rule_id
    assert len(crud.list_products(session)) == 1


def test_one_bad_entry_does_not_block_the_others(session: Session) -> None:
    good = DropManifestEntry.from_dict({"canonical_name": "Good Product", "ean": "444"})
    bad = DropManifestEntry.from_dict(
        {
            "canonical_name": "Bad Product",
            "merchant": "Kairyu",
            "product_url": "https://not-a-real-merchant.example/p/1",
        }
    )

    results = import_drop_manifest(session, [bad, good])

    assert results[0].status == "error"
    assert results[1].status == "created"


def test_product_url_entry_uses_the_real_connector(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from connectors.defaults import MerchantDefinition

    class _FakeConnector:
        def get_product(self, external_id: str) -> ConnectorProduct:
            return ConnectorProduct(
                external_id="etb-30e-fr",
                name="ETB 30e Anniversaire",
                price=Decimal("59.99"),
                currency="EUR",
                available=True,
                seller="Kairyu",
                url="https://kairyu.fr/products/etb-30e-fr",
            )

    fake_def = MerchantDefinition(
        name="Kairyu", domains=("kairyu.fr",), build_connector=lambda: _FakeConnector()
    )
    monkeypatch.setattr("app.drop_manifest.find_merchant_for_domain", lambda hostname: fake_def)

    entry = DropManifestEntry.from_dict(
        {
            "canonical_name": "ETB 30e Anniversaire",
            "merchant": "Kairyu",
            "product_url": "https://kairyu.fr/products/etb-30e-fr",
        }
    )

    results = import_drop_manifest(session, [entry])

    assert results[0].status == "created"
    assert results[0].listing_id is not None
    listing = crud.get_listing(session, results[0].listing_id)
    assert listing.external_id == "etb-30e-fr"
    assert listing.merchant.name == "Kairyu"


def test_product_url_fetch_failure_is_reported_not_raised(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from connectors.defaults import MerchantDefinition

    class _FailingConnector:
        def get_product(self, external_id: str) -> ConnectorProduct:
            raise ConnectorError("merchant is down")

    fake_def = MerchantDefinition(
        name="Kairyu", domains=("kairyu.fr",), build_connector=lambda: _FailingConnector()
    )
    monkeypatch.setattr("app.drop_manifest.find_merchant_for_domain", lambda hostname: fake_def)
    entry = DropManifestEntry.from_dict(
        {
            "canonical_name": "ETB 30e Anniversaire",
            "merchant": "Kairyu",
            "product_url": "https://kairyu.fr/products/etb-30e-fr",
        }
    )

    results = import_drop_manifest(session, [entry])

    assert results[0].status == "error"
    assert "merchant is down" in results[0].detail
