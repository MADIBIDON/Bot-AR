"""No real network: _build_discovery_registry is monkeypatched inside the
scripts.watch namespace, same pattern as _build_registry/_build_market_registry.
"""

from __future__ import annotations

import argparse
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

import scripts.watch as watch
from connectors.base import ConnectorProduct
from database import crud
from discovery.base import RetailDiscoverySource
from discovery.registry import DiscoveryRegistry


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    monkeypatch.setattr(watch, "_get_session", lambda: session)


class _FakeDiscoverySource(RetailDiscoverySource):
    def __init__(self, results: list[ConnectorProduct] | None = None) -> None:
        self._results = results or []

    def search(self, query, *, ean=None, mpn=None, limit=10):
        return self._results


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


def _fake_registry(**sources: RetailDiscoverySource) -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    for name, source in sources.items():
        registry.register(name, source)
    return registry


def _add_product_args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = dict(
        name="ETB Pokemon Chaos Ascendant FR",
        max_price="60",
        target_price=None,
        max_quantity=None,
        monitoring_interval=None,
        ean=None,
        gtin=None,
        mpn=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_add_product_creates_product_and_links_candidate(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(
        watch,
        "_build_discovery_registry",
        lambda: _fake_registry(Kairyu=_FakeDiscoverySource([_candidate()])),
    )

    rc = watch.cmd_add_product(_add_product_args())

    assert rc == 0
    products = crud.list_products(session)
    assert len(products) == 1
    assert products[0].max_price == Decimal("60")
    rules = crud.list_watch_rules(session, product_id=products[0].id)
    assert len(rules) == 1
    output = capsys.readouterr().out
    assert "LINKED" in output


def test_add_product_reuses_existing_product_by_ean(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    crud.create_product(session, "Existing", ean="1234567890123", max_price=Decimal("60"))
    monkeypatch.setattr(watch, "_build_discovery_registry", lambda: _fake_registry())

    rc = watch.cmd_add_product(_add_product_args(ean="1234567890123"))

    assert rc == 0
    assert len(crud.list_products(session)) == 1  # no duplicate created
    assert "Reusing existing product" in capsys.readouterr().out


def test_add_product_rejects_invalid_max_price(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)

    rc = watch.cmd_add_product(_add_product_args(max_price="-10"))

    assert rc == 1
    assert crud.list_products(session) == []


def test_products_lists_with_max_and_listing_count(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    crud.create_product(session, "ETB Pokemon Chaos Ascendant FR", max_price=Decimal("60"))

    rc = watch.cmd_products(argparse.Namespace())

    assert rc == 0
    output = capsys.readouterr().out
    assert "ETB Pokemon Chaos Ascendant FR" in output
    assert "60" in output


def test_products_empty_reports_none(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)

    rc = watch.cmd_products(argparse.Namespace())

    assert rc == 0
    assert "No products watched." in capsys.readouterr().out


def test_product_detail_shows_identifiers_and_listings(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    product = crud.create_product(
        session, "ETB Pokemon Chaos Ascendant FR", ean="1234567890123", max_price=Decimal("60")
    )

    rc = watch.cmd_product(argparse.Namespace(id=product.id))

    assert rc == 0
    output = capsys.readouterr().out
    assert "1234567890123" in output
    assert "none yet" in output


def test_product_missing_returns_error(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)

    assert watch.cmd_product(argparse.Namespace(id=999)) == 1


def test_discover_command_runs_discovery_for_existing_product(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    product = crud.create_product(
        session, "ETB Pokemon Chaos Ascendant FR", max_price=Decimal("60")
    )
    monkeypatch.setattr(
        watch,
        "_build_discovery_registry",
        lambda: _fake_registry(Kairyu=_FakeDiscoverySource([_candidate()])),
    )

    rc = watch.cmd_discover(argparse.Namespace(id=product.id))

    assert rc == 0
    assert len(crud.list_watch_rules(session, product_id=product.id)) == 1


def test_discover_missing_product_returns_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)

    assert watch.cmd_discover(argparse.Namespace(id=999)) == 1


def test_enable_disable_product_toggles_status_and_rules(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    product = crud.create_product(
        session, "ETB Pokemon Chaos Ascendant FR", max_price=Decimal("60")
    )
    merchant = crud.create_merchant(session, "Kairyu")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://kairyu.fr/products/etb-1",
        external_id="etb-1",
    )
    rule = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=60, max_quantity=1
    )

    assert watch.cmd_disable_product(argparse.Namespace(id=product.id)) == 0
    assert crud.get_product(session, product.id).status == "disabled"
    assert crud.get_watch_rule(session, rule.id).enabled is False

    assert watch.cmd_enable_product(argparse.Namespace(id=product.id)) == 0
    assert crud.get_product(session, product.id).status == "active"
    assert crud.get_watch_rule(session, rule.id).enabled is True


def test_disable_product_missing_returns_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)

    assert watch.cmd_disable_product(argparse.Namespace(id=999)) == 1


# --- Phase 25: edit-product (profitability config, per Product Watch) -----


def _edit_product_args(id: int, **overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = dict(
        target_price=None,
        max_price=None,
        estimated_resale_price=None,
        resale_trusted=None,
        platform_fee_pct=None,
        fixed_fee=None,
        shipping_cost=None,
        other_costs=None,
        resale_price_mode=None,
        market_source=None,
        minimum_net_profit=None,
        minimum_roi_pct=None,
        minimum_resale_confidence=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(id=id, **defaults)


def test_edit_product_updates_profitability_thresholds(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    product = crud.create_product(session, "ETB 30e", target_price=Decimal("56"))

    rc = watch.cmd_edit_product(
        _edit_product_args(
            product.id,
            minimum_net_profit="20",
            minimum_roi_pct="30",
            minimum_resale_confidence="medium",
        )
    )

    assert rc == 0
    updated = crud.get_product(session, product.id)
    assert updated.minimum_net_profit == Decimal("20")
    assert updated.minimum_roi_pct == Decimal("30")
    assert updated.minimum_resale_confidence == "medium"


def test_edit_product_sets_resale_trusted(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    product = crud.create_product(session, "ETB 30e", estimated_resale_price=Decimal("120"))

    rc = watch.cmd_edit_product(_edit_product_args(product.id, resale_trusted="true"))

    assert rc == 0
    assert crud.get_product(session, product.id).estimated_resale_trusted is True


def test_edit_product_rejects_invalid_resale_confidence(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    product = crud.create_product(session, "ETB 30e")

    rc = watch.cmd_edit_product(_edit_product_args(product.id, minimum_resale_confidence="extreme"))

    assert rc == 1


def test_edit_product_missing_returns_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)

    assert watch.cmd_edit_product(_edit_product_args(999)) == 1


def test_edit_product_with_no_fields_returns_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    product = crud.create_product(session, "ETB 30e")

    assert watch.cmd_edit_product(_edit_product_args(product.id)) == 1


def test_edit_product_market_mode_without_source_is_rejected(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    product = crud.create_product(session, "ETB 30e")

    rc = watch.cmd_edit_product(_edit_product_args(product.id, resale_price_mode="market"))

    assert rc == 1
