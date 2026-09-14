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
    assert "net profit: 18.70 EUR" in output
    assert "ROI: 24.97%" in output
    assert "status: buy_candidate" in output


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
        resale_price_mode=None,
        market_source=None,
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


# --- market data (Phase 16) -------------------------------------------

from market_data.base import MarketDataError, MarketDataSource  # noqa: E402
from market_data.ebay import MissingEbayConfigError  # noqa: E402
from market_data.models import MarketObservation  # noqa: E402
from market_data.registry import MarketDataRegistry  # noqa: E402


class _FakeMarketSource(MarketDataSource):
    def __init__(
        self, observations: list[MarketObservation] | None = None, *, error: bool = False
    ) -> None:
        self._observations = observations or []
        self._error = error

    def search(self, query: str, *, limit: int = 20) -> list[MarketObservation]:
        if self._error:
            raise MarketDataError("simulated")
        return self._observations


@pytest.fixture(autouse=True)
def _clear_market_cache() -> None:
    """The CLI's module-level TTLCache is process-lifetime by design (see
    scripts/watch.py), but that means tests must not leak cached estimates
    into each other via the shared "source:product_name" cache key."""
    watch._market_cache.clear()
    yield
    watch._market_cache.clear()


def _market_obs(price: str, name: str = "Duopack Evoli") -> MarketObservation:
    from datetime import UTC, datetime

    return MarketObservation(
        source="ebay",
        product_name=name,
        price=Decimal(price),
        currency="EUR",
        listing_url="https://ebay.example/1",
        external_id="1",
        observed_at=datetime.now(UTC),
    )


def test_edit_sets_market_mode_and_source(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(_edit_args(rule_id, resale_price_mode="market", market_source="ebay"))

    assert rc == 0
    rule = crud.get_watch_rule(session, rule_id)
    assert rule.resale_price_mode == "market"
    assert rule.market_source == "ebay"


def test_edit_market_mode_without_source_is_rejected(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(_edit_args(rule_id, resale_price_mode="market"))

    assert rc == 1


def test_edit_rejects_unsupported_market_source(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_edit(_edit_args(rule_id, market_source="cardmarket"))

    assert rc == 1


def test_market_command_requires_market_mode(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)

    rc = watch.cmd_market(argparse.Namespace(id=rule_id))

    assert rc == 1


def test_market_command_reports_diagnostics(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)
    watch.cmd_edit(_edit_args(rule_id, resale_price_mode="market", market_source="ebay"))

    registry = MarketDataRegistry()
    registry.register(
        "ebay", _FakeMarketSource([_market_obs("90"), _market_obs("100"), _market_obs("110")])
    )
    monkeypatch.setattr(watch, "_build_market_registry", lambda: registry)

    rc = watch.cmd_market(argparse.Namespace(id=rule_id))

    assert rc == 0
    output = capsys.readouterr().out
    assert "Market source: ebay" in output
    assert "Matched observations: 3" in output
    assert "Median: 100" in output


def test_market_command_missing_config_reports_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule(session)
    watch.cmd_edit(_edit_args(rule_id, resale_price_mode="market", market_source="ebay"))

    def raise_missing() -> None:
        raise MissingEbayConfigError("Missing required environment variable(s): EBAY_APP_ID")

    monkeypatch.setattr(watch, "_build_market_registry", raise_missing)

    rc = watch.cmd_market(argparse.Namespace(id=rule_id))

    assert rc == 1


def test_test_command_market_mode_shows_market_estimate(
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
    crud.update_watch_rule(session, rule.id, resale_price_mode="market", market_source="ebay")

    def fake_registry() -> ConnectorRegistry:
        registry = ConnectorRegistry()
        registry.register(
            "FakeStore",
            FakeStoreConnector(
                products={
                    "fake-1": {
                        "name": "Duopack Evoli",
                        "price": 60.0,
                        "available": True,
                        "seller": "FakeStore",
                        "url": "https://a.example/p/1",
                    }
                }
            ),
        )
        return registry

    market_registry = MarketDataRegistry()
    market_registry.register(
        "ebay", _FakeMarketSource([_market_obs("90"), _market_obs("100"), _market_obs("110")])
    )

    monkeypatch.setattr(watch, "_build_registry", fake_registry)
    monkeypatch.setattr(watch, "_build_market_registry", lambda: market_registry)
    monkeypatch.setattr(watch, "load_discord_config", lambda: DiscordConfig("x", 1, 1))
    monkeypatch.setattr(watch, "DiscordNotifier", _FakeAsyncNotifier)

    rc = watch.cmd_test(argparse.Namespace(id=rule.id))

    assert rc == 0
    output = capsys.readouterr().out
    assert "Market estimate:" in output
    assert "source: ebay" in output
    assert "Opportunity:" in output
    assert "purchase price: 60.0 EUR" in output


def test_test_command_market_mode_unavailable_falls_back_gracefully(
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
    crud.update_watch_rule(session, rule.id, resale_price_mode="market", market_source="ebay")

    def fake_registry() -> ConnectorRegistry:
        registry = ConnectorRegistry()
        registry.register(
            "FakeStore",
            FakeStoreConnector(
                products={
                    "fake-1": {
                        "name": "Duopack Evoli",
                        "price": 60.0,
                        "available": True,
                        "seller": "FakeStore",
                        "url": "https://a.example/p/1",
                    }
                }
            ),
        )
        return registry

    def raise_missing() -> None:
        raise MissingEbayConfigError("Missing required environment variable(s): EBAY_APP_ID")

    monkeypatch.setattr(watch, "_build_registry", fake_registry)
    monkeypatch.setattr(watch, "_build_market_registry", raise_missing)
    monkeypatch.setattr(watch, "load_discord_config", lambda: DiscordConfig("x", 1, 1))
    monkeypatch.setattr(watch, "DiscordNotifier", _FakeAsyncNotifier)

    rc = watch.cmd_test(argparse.Namespace(id=rule.id))

    assert rc == 0
    output = capsys.readouterr().out
    assert "market data unavailable" in output


# --- opportunities (Phase 17) -------------------------------------------


def _opportunities_args(**overrides: object) -> argparse.Namespace:
    defaults = dict(top=None, min_priority=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _seed_rule_with_observation(
    session: Session,
    *,
    product_name: str,
    external_id: str,
    price: str,
    merchant_name: str = "RetailerA",
) -> int:
    from datetime import UTC, datetime

    from products.observation import ProductObservation

    product = crud.create_product(session, product_name)
    merchant = crud.get_merchant_by_name(session, merchant_name)
    if merchant is None:
        merchant = crud.create_merchant(session, merchant_name)
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url=f"https://a.example/p/{external_id}",
        external_id=external_id,
    )
    rule = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=60, max_quantity=1
    )
    obs = ProductObservation(
        merchant=merchant_name,
        external_id=external_id,
        name=product_name,
        price=Decimal(price),
        currency="EUR",
        available=True,
        url=f"https://a.example/p/{external_id}",
        observed_at=datetime.now(UTC),
    )
    crud.create_observation_record(session, listing_id=listing.id, observation=obs)
    return rule.id


def test_opportunities_with_no_rules_reports_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)

    rc = watch.cmd_opportunities(_opportunities_args())

    assert rc == 0
    assert "No rankable opportunities." in capsys.readouterr().out


def test_opportunities_ranks_manual_mode_rules(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)

    good_id = _seed_rule_with_observation(
        session, product_name="Great Deal", external_id="good-1", price="50"
    )
    crud.update_watch_rule(session, good_id, estimated_resale_price=Decimal("150"))

    bad_id = _seed_rule_with_observation(
        session, product_name="Bad Deal", external_id="bad-1", price="90"
    )
    crud.update_watch_rule(session, bad_id, estimated_resale_price=Decimal("95"))

    rc = watch.cmd_opportunities(_opportunities_args())

    assert rc == 0
    output = capsys.readouterr().out
    assert "Great Deal" in output
    assert "Bad Deal" in output
    # Better ROI should be listed first.
    assert output.index("Great Deal") < output.index("Bad Deal")


def test_opportunities_skips_rules_without_estimate(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    _seed_rule_with_observation(
        session, product_name="No Estimate", external_id="none-1", price="50"
    )

    rc = watch.cmd_opportunities(_opportunities_args())

    assert rc == 0
    output = capsys.readouterr().out
    assert "No Estimate" in output
    assert "no resale estimate available" in output


def test_opportunities_skips_rules_with_no_data_yet(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    product = crud.create_product(session, "Never Checked")
    merchant = crud.create_merchant(session, "RetailerB")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://a.example/p/never",
        external_id="never-1",
    )
    crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=60, max_quantity=1
    )

    rc = watch.cmd_opportunities(_opportunities_args())

    assert rc == 0
    output = capsys.readouterr().out
    assert "1 rule(s) skipped: no monitoring data yet." in output
    assert "No rankable opportunities." in output


def test_opportunities_top_limits_results(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    for i in range(3):
        rule_id = _seed_rule_with_observation(
            session, product_name=f"Product {i}", external_id=f"p-{i}", price="50"
        )
        crud.update_watch_rule(session, rule_id, estimated_resale_price=Decimal(str(100 + i * 10)))

    rc = watch.cmd_opportunities(_opportunities_args(top=1))

    assert rc == 0
    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if " | " in line]
    # header + exactly one data row
    assert len(lines) == 2


def test_opportunities_min_priority_filters_low_scores(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    weak_id = _seed_rule_with_observation(
        session, product_name="Barely Profitable", external_id="weak-1", price="95"
    )
    crud.update_watch_rule(session, weak_id, estimated_resale_price=Decimal("96"))

    rc = watch.cmd_opportunities(_opportunities_args(min_priority="top"))

    assert rc == 0
    output = capsys.readouterr().out
    assert "No rankable opportunities." in output


def test_opportunities_market_mode_without_credentials_shows_market_unavailable(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    rule_id = _seed_rule_with_observation(
        session, product_name="Market Product", external_id="market-1", price="50"
    )
    crud.update_watch_rule(session, rule_id, resale_price_mode="market", market_source="ebay")

    def raise_missing() -> None:
        raise MissingEbayConfigError("Missing required environment variable(s): EBAY_APP_ID")

    monkeypatch.setattr(watch, "_build_market_registry", raise_missing)

    rc = watch.cmd_opportunities(_opportunities_args())

    assert rc == 0
    output = capsys.readouterr().out
    assert "market unavailable" in output
    assert "Market Product" in output


def test_opportunities_does_not_crash_without_ebay_and_mixes_manual_rules(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    manual_id = _seed_rule_with_observation(
        session, product_name="Manual Product", external_id="manual-1", price="50"
    )
    crud.update_watch_rule(session, manual_id, estimated_resale_price=Decimal("150"))

    market_id = _seed_rule_with_observation(
        session, product_name="Market Product", external_id="market-2", price="50"
    )
    crud.update_watch_rule(session, market_id, resale_price_mode="market", market_source="ebay")

    def raise_missing() -> None:
        raise MissingEbayConfigError("Missing required environment variable(s): EBAY_APP_ID")

    monkeypatch.setattr(watch, "_build_market_registry", raise_missing)

    rc = watch.cmd_opportunities(_opportunities_args())

    assert rc == 0
    output = capsys.readouterr().out
    assert "Manual Product" in output
    assert "Market Product" in output


# --- purchases (Phase 19) -----------------------------------------------


def _seed_purchase_rule(
    session: Session, *, price: str = "59.90", max_price: str = "60", external_id: str = "etb-1"
) -> tuple[int, int]:
    product = crud.create_product(session, "ETB Chaos Ascendant FR")
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
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=1,
        max_quantity=1,
        max_price=Decimal(max_price),
    )
    return rule.id, listing.id


def test_purchases_with_no_history_reports_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)

    rc = watch.cmd_purchases(argparse.Namespace(limit=None))

    assert rc == 0
    assert "No purchase attempts recorded." in capsys.readouterr().out


def test_purchases_lists_recorded_attempts(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    rule_id, listing_id = _seed_purchase_rule(session)
    crud.create_purchase_attempt(
        session,
        watch_rule_id=rule_id,
        listing_id=listing_id,
        product_id=crud.get_watch_rule(session, rule_id).product_id,
        status="failed",
        observed_price=Decimal("59.90"),
        max_price_allowed=Decimal("60"),
        quantity=1,
    )

    rc = watch.cmd_purchases(argparse.Namespace(limit=None))

    assert rc == 0
    output = capsys.readouterr().out
    assert "failed" in output


def test_purchase_status_reports_policy_and_history(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    monkeypatch.setenv("PURCHASES_ENABLED", "false")
    monkeypatch.delenv("PURCHASE_ALLOWED_MERCHANTS", raising=False)

    rc = watch.cmd_purchase_status(argparse.Namespace())

    assert rc == 0
    output = capsys.readouterr().out
    assert "PURCHASES_ENABLED:          False" in output
    assert "Most recent attempt:       none" in output


def test_purchase_test_missing_listing_returns_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, session)

    rc = watch.cmd_purchase_test(argparse.Namespace(listing_id=999))

    assert rc == 1


def test_purchase_test_dry_run_would_purchase_yes(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    _rule_id, listing_id = _seed_purchase_rule(session, price="59.90", max_price="60")
    monkeypatch.setenv("PURCHASES_ENABLED", "true")
    monkeypatch.setenv("PURCHASE_ALLOWED_MERCHANTS", "retailera.example")
    monkeypatch.setattr(watch, "domains_for_merchant", lambda name: ("retailera.example",))

    def fake_registry() -> ConnectorRegistry:
        registry = ConnectorRegistry()
        registry.register(
            "RetailerA",
            FakeStoreConnector(
                products={
                    "etb-1": {
                        "name": "ETB Chaos Ascendant FR",
                        "price": 59.90,
                        "available": True,
                        "seller": "RetailerA",
                        "url": "https://a.example/p/etb-1",
                    }
                }
            ),
        )
        return registry

    monkeypatch.setattr(watch, "_build_registry", fake_registry)

    rc = watch.cmd_purchase_test(argparse.Namespace(listing_id=listing_id))

    assert rc == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "WOULD PURCHASE: YES" in output
    assert "No transaction executed." in output
    # A dry run must never create a PurchaseAttempt row.
    assert crud.list_purchase_attempts(session) == []


def test_purchase_test_dry_run_would_purchase_no_when_disabled(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_session(monkeypatch, session)
    _rule_id, listing_id = _seed_purchase_rule(session)
    monkeypatch.setenv("PURCHASES_ENABLED", "false")

    def fake_registry() -> ConnectorRegistry:
        registry = ConnectorRegistry()
        registry.register(
            "RetailerA",
            FakeStoreConnector(
                products={
                    "etb-1": {
                        "name": "ETB Chaos Ascendant FR",
                        "price": 59.90,
                        "available": True,
                        "seller": "RetailerA",
                        "url": "https://a.example/p/etb-1",
                    }
                }
            ),
        )
        return registry

    monkeypatch.setattr(watch, "_build_registry", fake_registry)

    rc = watch.cmd_purchase_test(argparse.Namespace(listing_id=listing_id))

    assert rc == 0
    output = capsys.readouterr().out
    assert "WOULD PURCHASE: NO" in output
    assert "PURCHASES_ENABLED" in output
    assert crud.list_purchase_attempts(session) == []
