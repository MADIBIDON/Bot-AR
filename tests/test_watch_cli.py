"""No real network, no real Discord: merchant detection, _build_registry
and DiscordNotifier are all monkeypatched inside the scripts.watch
namespace.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import scripts.watch as watch
from connectors.base import ConnectorError, ConnectorProduct
from connectors.defaults import MerchantDefinition
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


class _FakeConnectorSuccess:
    def __init__(self, *, merchant_name: str) -> None:
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


class _FakeConnectorFailure:
    def get_product(self, external_id: str) -> ConnectorProduct:
        raise ConnectorError("simulated: page structure changed")


def _fake_merchant_success(name: str = "Kairyu") -> MerchantDefinition:
    return MerchantDefinition(
        name=name,
        domains=("kairyu.fr",),
        build_connector=lambda: _FakeConnectorSuccess(merchant_name=name),
    )


def _fake_merchant_failure() -> MerchantDefinition:
    return MerchantDefinition(
        name="Kairyu", domains=("kairyu.fr",), build_connector=_FakeConnectorFailure
    )


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


def test_add_with_detected_product(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "find_merchant_for_domain", lambda host: _fake_merchant_success())

    rc = watch.cmd_add(_args(url="https://kairyu.fr/products/some-handle"))

    assert rc == 0
    rules = crud.list_watch_rules(session)
    assert len(rules) == 1
    assert rules[0].product.name == "Elite Trainer Box Detected"
    assert rules[0].product.ean == "1234567890123"
    assert rules[0].listing.external_id == "detected-handle"
    assert rules[0].listing.merchant.name == "Kairyu"


def test_add_falls_back_to_manual_on_unsupported_domain(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "find_merchant_for_domain", lambda host: None)

    rc = watch.cmd_add(
        _args(
            url="https://not-supported.test/p/1",
            merchant="ManualShop",
            name="Manual Product",
            external_id="manual-1",
        )
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "Unsupported domain" in output
    assert "not-supported.test" in output
    rules = crud.list_watch_rules(session)
    assert rules[0].product.name == "Manual Product"
    assert rules[0].product.ean is None
    assert rules[0].listing.external_id == "manual-1"


def test_add_falls_back_to_manual_when_connector_fails(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "find_merchant_for_domain", lambda host: _fake_merchant_failure())

    rc = watch.cmd_add(
        _args(
            url="https://kairyu.fr/products/some-handle",
            name="Manual Product",
            external_id="manual-1",
        )
    )

    assert rc == 0
    rules = crud.list_watch_rules(session)
    assert rules[0].product.name == "Manual Product"
    assert rules[0].product.ean is None


def test_add_reuses_existing_listing_for_same_merchant_and_external_id(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "find_merchant_for_domain", lambda host: _fake_merchant_success())

    watch.cmd_add(_args(url="https://kairyu.fr/products/some-handle"))
    watch.cmd_add(_args(url="https://kairyu.fr/products/some-handle", target_price="10"))

    listings = crud.list_listings_for_product(session, crud.list_watch_rules(session)[0].product_id)
    assert len(listings) == 1  # reused, not duplicated
    assert len(crud.list_watch_rules(session)) == 2  # two separate rules on the same listing


def test_add_rejects_negative_target_price(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "find_merchant_for_domain", lambda host: _fake_merchant_success())

    rc = watch.cmd_add(_args(url="https://kairyu.fr/products/some-handle", target_price="-5"))

    assert rc == 1
    assert crud.list_watch_rules(session) == []


def test_add_rejects_zero_check_interval(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "find_merchant_for_domain", lambda host: _fake_merchant_success())

    rc = watch.cmd_add(_args(url="https://kairyu.fr/products/some-handle", check_interval="0"))

    assert rc == 1
    assert crud.list_watch_rules(session) == []


def _seed_rule(session: Session, *, external_id: str = "fake-1") -> int:
    product = crud.create_product(session, "Duopack Evoli")
    merchant = crud.get_merchant_by_name(session, "RetailerA")
    if merchant is None:
        merchant = crud.create_merchant(session, "RetailerA")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url=f"https://a.example/p/{external_id}",
        external_id=external_id,
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


def test_test_command_displays_opportunity_when_configured(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    crud.update_watch_rule(
        session,
        rule.id,
        estimated_resale_price=Decimal("110"),
        platform_fee_pct=Decimal("9"),
        shipping_cost=Decimal("6.50"),
    )

    def fake_registry() -> ConnectorRegistry:
        registry = ConnectorRegistry()
        registry.register(
            "FakeStore",
            FakeStoreConnector(
                products={
                    "fake-1": {
                        "name": "Duopack Evoli",
                        "price": 74.90,
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
    output = capsys.readouterr().out
    assert "Opportunity:" in output
    assert "net profit:       18.70 EUR" in output
    assert "ROI:              24.97%" in output
    assert "status:           buy_candidate" in output


def test_test_command_reports_opportunity_not_configured(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)

    product = crud.create_product(session, "Duopack Evoli")
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
    output = capsys.readouterr().out
    assert "Opportunity:" in output
    assert "not configured" in output


# --- edit -------------------------------------------------------------


def _edit_args(id: int, **overrides: object) -> argparse.Namespace:
    defaults = dict(
        target_price=None,
        max_price=None,
        check_interval=None,
        max_quantity=None,
        estimated_resale_price=None,
        platform_fee_pct=None,
        fixed_fee=None,
        shipping_cost=None,
        other_costs=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(id=id, **defaults)


def test_edit_updates_target_and_max_price(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(_edit_args(rule_id, target_price="55", max_price="65"))

    assert rc == 0
    rule = crud.get_watch_rule(session, rule_id)
    assert rule.target_price == Decimal("55")
    assert rule.max_price == Decimal("65")


def test_edit_updates_check_interval(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(_edit_args(rule_id, check_interval="120"))

    assert rc == 0
    assert crud.get_watch_rule(session, rule_id).check_interval == 120


def test_edit_rejects_negative_price(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)
    original = crud.get_watch_rule(session, rule_id).target_price

    rc = watch.cmd_edit(_edit_args(rule_id, target_price="-10"))

    assert rc == 1
    assert crud.get_watch_rule(session, rule_id).target_price == original


def test_edit_rejects_max_price_below_target_price(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)
    watch.cmd_edit(_edit_args(rule_id, target_price="60"))

    rc = watch.cmd_edit(_edit_args(rule_id, max_price="50"))

    assert rc == 1
    assert crud.get_watch_rule(session, rule_id).max_price is None


def test_edit_rejects_check_interval_below_minimum(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(_edit_args(rule_id, check_interval="5"))

    assert rc == 1
    assert crud.get_watch_rule(session, rule_id).check_interval == 1


def test_edit_missing_rule_returns_error(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    assert watch.cmd_edit(_edit_args(999, target_price="10")) == 1


# --- edit: opportunity fields (Phase 15) --------------------------------


def test_edit_updates_opportunity_fields(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(
        _edit_args(
            rule_id,
            estimated_resale_price="110",
            platform_fee_pct="9",
            shipping_cost="6.50",
        )
    )

    assert rc == 0
    rule = crud.get_watch_rule(session, rule_id)
    assert rule.estimated_resale_price == Decimal("110")
    assert rule.platform_fee_pct == Decimal("9")
    assert rule.shipping_cost == Decimal("6.50")
    assert rule.fixed_fee is None
    assert rule.other_costs is None


def test_edit_rejects_negative_estimated_resale_price(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(_edit_args(rule_id, estimated_resale_price="-10"))

    assert rc == 1
    assert crud.get_watch_rule(session, rule_id).estimated_resale_price is None


def test_edit_rejects_platform_fee_pct_at_100(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(_edit_args(rule_id, platform_fee_pct="100"))

    assert rc == 1
    assert crud.get_watch_rule(session, rule_id).platform_fee_pct is None


def test_edit_rejects_negative_shipping_cost(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(_edit_args(rule_id, shipping_cost="-1"))

    assert rc == 1


def test_edit_allows_zero_shipping_cost(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(_edit_args(rule_id, shipping_cost="0"))

    assert rc == 0
    assert crud.get_watch_rule(session, rule_id).shipping_cost == Decimal("0")


def test_edit_with_no_fields_returns_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    assert watch.cmd_edit(_edit_args(rule_id)) == 1


# --- delete message -----------------------------------------------------


def test_delete_blocked_message_is_exact(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)

    from datetime import UTC, datetime

    from products.observation import ProductObservation

    obs = ProductObservation(
        merchant="RetailerA",
        external_id="fake-1",
        name="Duopack Evoli",
        price=Decimal("13.99"),
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

    watch.cmd_delete(argparse.Namespace(id=rule_id))

    output = capsys.readouterr().out
    assert "Cannot delete watch rule because historical events exist." in output
    assert f"Use `disable {rule_id}` to stop monitoring while preserving history." in output


# --- status ---------------------------------------------------------------


def test_status_with_no_data(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(watch, "PID_FILE", Path("/nonexistent/worker.pid"))

    rc = watch.cmd_status(argparse.Namespace())

    assert rc == 0


def test_status_reports_active_and_disabled_counts(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    rule_id_a = _seed_rule(session, external_id="fake-1")
    _seed_rule(session, external_id="fake-2")
    watch.cmd_disable(argparse.Namespace(id=rule_id_a))
    monkeypatch.setattr(watch, "PID_FILE", Path("/nonexistent/worker.pid"))

    watch.cmd_status(argparse.Namespace())

    output = capsys.readouterr().out
    assert "active rules:   1" in output
    assert "disabled rules: 1" in output
    assert "worker:         not running" in output


def test_status_reports_last_check_and_event(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    monkeypatch.setattr(watch, "PID_FILE", Path("/nonexistent/worker.pid"))

    from datetime import UTC, datetime

    from products.observation import ProductObservation

    obs = ProductObservation(
        merchant="RetailerA",
        external_id="fake-1",
        name="Duopack Evoli",
        price=Decimal("13.99"),
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

    watch.cmd_status(argparse.Namespace())

    output = capsys.readouterr().out
    assert "last check:" in output
    assert "never" not in output.split("last check:")[1].splitlines()[0]
    assert "last event:" in output
    assert "price_drop" in output
