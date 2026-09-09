"""No real network, no real Discord: ShopifyConnector, _build_registry and
DiscordNotifier are all monkeypatched inside the scripts.watch namespace.
"""

from __future__ import annotations

import argparse
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

import scripts.watch as watch
from connectors.base import ConnectorError, ConnectorProduct
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from database import crud
from notifications.discord.config import DiscordConfig


class _FakeAsyncNotifier:
    def __init__(self, config: object) -> None:
        self.sent: list[object] = []

    async def __aenter__(self) -> _FakeAsyncNotifier:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def send_embed(self, embed: object) -> None:
        self.sent.append(embed)


class _FakeShopifyConnectorSuccess:
    def __init__(self, *, shop_domain: str, merchant_name: str) -> None:
        self.shop_domain = shop_domain
        self.merchant_name = merchant_name

    def get_product(self, external_id: str) -> ConnectorProduct:
        return ConnectorProduct(
            external_id="detected-handle",
            name="Elite Trainer Box Detected",
            price=Decimal("59.99"),
            currency="EUR",
            available=True,
            seller=self.merchant_name,
            url="https://example-shop.test/products/detected-handle",
            ean="1234567890123",
            mpn=None,
        )


class _FakeShopifyConnectorFailure:
    def __init__(self, *, shop_domain: str, merchant_name: str) -> None:
        pass

    def get_product(self, external_id: str) -> ConnectorProduct:
        raise ConnectorError("simulated: not a shopify page")


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    monkeypatch.setattr(watch, "_get_session", lambda: session)


def _args(**overrides: object) -> argparse.Namespace:
    defaults = dict(
        url=None,
        merchant=None,
        name=None,
        external_id=None,
        target_price=None,
        max_price=None,
        check_interval=None,
        max_quantity=None,
        yes=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_add_with_detected_shopify_product(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "ShopifyConnector", _FakeShopifyConnectorSuccess)

    rc = watch.cmd_add(_args(url="https://kairyu.fr/products/some-handle", merchant="Kairyu"))

    assert rc == 0
    rules = crud.list_watch_rules(session)
    assert len(rules) == 1
    assert rules[0].product.name == "Elite Trainer Box Detected"
    assert rules[0].product.ean == "1234567890123"
    assert rules[0].listing.external_id == "detected-handle"
    assert rules[0].listing.merchant.name == "Kairyu"


def test_add_falls_back_to_manual_when_detection_fails(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "ShopifyConnector", _FakeShopifyConnectorFailure)

    rc = watch.cmd_add(
        _args(
            url="https://not-shopify.test/p/1",
            merchant="ManualShop",
            name="Manual Product",
            external_id="manual-1",
        )
    )

    assert rc == 0
    rules = crud.list_watch_rules(session)
    assert rules[0].product.name == "Manual Product"
    assert rules[0].product.ean is None
    assert rules[0].listing.external_id == "manual-1"


def test_add_reuses_existing_listing_for_same_merchant_and_external_id(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "ShopifyConnector", _FakeShopifyConnectorSuccess)

    watch.cmd_add(_args(url="https://kairyu.fr/products/some-handle", merchant="Kairyu"))
    watch.cmd_add(
        _args(
            url="https://kairyu.fr/products/some-handle",
            merchant="Kairyu",
            target_price="10",
        )
    )

    listings = crud.list_listings_for_product(session, crud.list_watch_rules(session)[0].product_id)
    assert len(listings) == 1  # reused, not duplicated
    assert len(crud.list_watch_rules(session)) == 2  # two separate rules on the same listing


def test_add_rejects_negative_target_price(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "ShopifyConnector", _FakeShopifyConnectorSuccess)

    rc = watch.cmd_add(
        _args(url="https://kairyu.fr/products/some-handle", merchant="Kairyu", target_price="-5")
    )

    assert rc == 1
    assert crud.list_watch_rules(session) == []


def test_add_rejects_zero_check_interval(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "ShopifyConnector", _FakeShopifyConnectorSuccess)

    rc = watch.cmd_add(
        _args(
            url="https://kairyu.fr/products/some-handle",
            merchant="Kairyu",
            check_interval="0",
        )
    )

    assert rc == 1
    assert crud.list_watch_rules(session) == []


def _seed_rule(session: Session) -> int:
    product = crud.create_product(session, "Duopack Evoli")
    merchant = crud.create_merchant(session, "RetailerA")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://a.example/p/1",
        external_id="fake-1",
    )
    rule = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=1, max_quantity=1
    )
    return rule.id


def test_list_show_enable_disable(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    assert watch.cmd_list(argparse.Namespace(status="all")) == 0
    assert watch.cmd_show(argparse.Namespace(id=rule_id)) == 0

    assert watch.cmd_disable(argparse.Namespace(id=rule_id)) == 0
    assert crud.get_watch_rule(session, rule_id).enabled is False

    assert watch.cmd_enable(argparse.Namespace(id=rule_id)) == 0
    assert crud.get_watch_rule(session, rule_id).enabled is True


def test_show_missing_rule_returns_error(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    assert watch.cmd_show(argparse.Namespace(id=999)) == 1


def test_delete_removes_rule(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    assert watch.cmd_delete(argparse.Namespace(id=rule_id)) == 0
    assert crud.get_watch_rule(session, rule_id) is None


def test_delete_blocked_when_events_exist(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)

    from datetime import UTC, datetime
    from decimal import Decimal as D

    from products.observation import ProductObservation

    obs = ProductObservation(
        merchant="RetailerA",
        external_id="fake-1",
        name="Duopack Evoli",
        price=D("13.99"),
        currency="EUR",
        available=True,
        url="https://a.example/p/1",
        observed_at=datetime.now(UTC),
    )
    record = crud.create_observation_record(session, listing_id=rule.listing_id, observation=obs)
    crud.create_event_record(
        session,
        event_type="price_drop",
        listing_id=rule.listing_id,
        watch_rule_id=rule.id,
        observation_record_id=record.id,
        occurred_at=datetime.now(UTC),
    )

    rc = watch.cmd_delete(argparse.Namespace(id=rule_id))

    assert rc == 1
    assert crud.get_watch_rule(session, rule_id) is not None


def test_test_command_runs_check_and_reports(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)

    product = crud.create_product(session, "Duopack Evoli", ean="1234567890123")
    merchant = crud.create_merchant(session, "FakeStore")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://a.example/p/1",
        external_id="fake-1",
    )
    rule = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=1, max_quantity=1
    )

    def fake_registry() -> ConnectorRegistry:
        registry = ConnectorRegistry()
        registry.register(
            "FakeStore",
            FakeStoreConnector(
                products={
                    "fake-1": {
                        "name": "Duopack Evoli",
                        "price": 13.99,
                        "available": True,
                        "seller": "FakeStore",
                        "url": "https://a.example/p/1",
                        "ean": "1234567890123",
                    }
                }
            ),
        )
        return registry

    monkeypatch.setattr(watch, "_build_registry", fake_registry)
    monkeypatch.setattr(watch, "load_discord_config", lambda: DiscordConfig("x", 1, 1))
    monkeypatch.setattr(watch, "DiscordNotifier", _FakeAsyncNotifier)

    rc = watch.cmd_test(argparse.Namespace(id=rule.id))

    assert rc == 0
    # first ever check: no previous observation, so no events, no notification
    assert len(crud.list_observation_records_for_listing(session, listing.id)) == 1
