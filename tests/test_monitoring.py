from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from connectors.base import ConnectorError
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from database import crud
from engine.monitoring import run_all_active_watch_rules, run_check, run_check_and_store


def _setup_rule(
    session: Session,
    *,
    merchant_name: str = "RetailerA",
    merchant=None,
    product_ean: str | None = None,
    external_id: str = "fake-123",
    enabled: bool = True,
    with_listing: bool = True,
):
    product = crud.create_product(session, "Duopack Evoli 30 ans", ean=product_ean)
    if merchant is None:
        merchant = crud.create_merchant(session, merchant_name)
    listing = None
    if with_listing:
        listing = crud.create_listing(
            session,
            product_id=product.id,
            merchant_id=merchant.id,
            url="https://a.example/p/1",
            external_id=external_id,
        )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id if listing else None,
        check_interval=300,
        max_quantity=1,
        enabled=enabled,
    )
    return product, merchant, listing, rule


def _registry_with_fake_store(merchant_name: str, **fake_store_kwargs: object) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    registry.register(merchant_name, FakeStoreConnector(**fake_store_kwargs))
    return registry


def test_active_rule_product_available(session: Session) -> None:
    _, _, _, rule = _setup_rule(session)
    registry = _registry_with_fake_store(
        "RetailerA",
        products={
            "fake-123": {
                "name": "Duopack Evoli 30 ans",
                "price": 13.99,
                "available": True,
                "seller": "RetailerA",
                "url": "https://a.example/p/1",
            }
        },
    )

    result = run_check(rule, registry)

    assert result.success is True
    assert result.error is None
    assert result.observation is not None
    assert result.observation.available is True


def test_active_rule_product_unavailable(session: Session) -> None:
    _, _, _, rule = _setup_rule(session)
    registry = _registry_with_fake_store(
        "RetailerA",
        products={
            "fake-123": {
                "name": "Duopack Evoli 30 ans",
                "price": 13.99,
                "available": False,
                "seller": "RetailerA",
                "url": "https://a.example/p/1",
            }
        },
    )

    result = run_check(rule, registry)

    assert result.success is True
    assert result.observation.available is False


def test_price_is_decimal(session: Session) -> None:
    _, _, _, rule = _setup_rule(session)
    registry = _registry_with_fake_store(
        "RetailerA",
        products={
            "fake-123": {
                "name": "Duopack Evoli 30 ans",
                "price": 13.99,
                "available": True,
                "seller": "RetailerA",
                "url": "https://a.example/p/1",
            }
        },
    )

    result = run_check(rule, registry)

    assert result.observation.price == Decimal("13.99")
    assert isinstance(result.observation.price, Decimal)


def test_observation_is_normalized(session: Session) -> None:
    _, _, listing, rule = _setup_rule(session)
    registry = _registry_with_fake_store(
        "RetailerA",
        products={
            "fake-123": {
                "name": "Duopack Evoli 30 ans",
                "price": 13.99,
                "available": True,
                "seller": "RetailerA",
                "url": "https://a.example/p/1",
                "ean": "1234567890123",
            }
        },
    )

    result = run_check(rule, registry)

    assert result.observation.merchant == "RetailerA"
    assert result.observation.external_id == "fake-123"
    assert result.observation.name == "Duopack Evoli 30 ans"
    assert result.observation.ean == "1234567890123"
    assert result.observation.observed_at.tzinfo is not None


def test_matcher_is_run_with_ean_exact(session: Session) -> None:
    _, _, _, rule = _setup_rule(session, product_ean="1234567890123")
    registry = _registry_with_fake_store(
        "RetailerA",
        products={
            "fake-123": {
                "name": "Duopack Evoli 30 ans",
                "price": 13.99,
                "available": True,
                "seller": "RetailerA",
                "url": "https://a.example/p/1",
                "ean": "1234567890123",
            }
        },
    )

    result = run_check(rule, registry)

    assert result.match_result is not None
    assert result.match_result.matched is True
    assert result.match_result.confidence == 100
    assert result.match_result.method == "ean_exact"


def test_matcher_negative_on_wrong_ean(session: Session) -> None:
    _, _, _, rule = _setup_rule(session, product_ean="1234567890123")
    registry = _registry_with_fake_store(
        "RetailerA",
        products={
            "fake-123": {
                "name": "Duopack Evoli 30 ans",
                "price": 13.99,
                "available": True,
                "seller": "RetailerA",
                "url": "https://a.example/p/1",
                "ean": "9999999999999",
            }
        },
    )

    result = run_check(rule, registry)

    assert result.match_result.matched is False
    assert result.match_result.method == "ean_mismatch"


def test_connector_error_does_not_crash(session: Session) -> None:
    _, _, _, rule = _setup_rule(session)
    registry = _registry_with_fake_store(
        "RetailerA",
        errors={"fake-123": ConnectorError("merchant is down")},
    )

    result = run_check(rule, registry)

    assert result.success is False
    assert result.observation is None
    assert "merchant is down" in result.error


def test_product_not_found(session: Session) -> None:
    _, _, _, rule = _setup_rule(session)
    registry = _registry_with_fake_store("RetailerA", products={})

    result = run_check(rule, registry)

    assert result.success is False
    assert result.observation is None
    assert result.error is not None


def test_disabled_rule_is_not_executed(session: Session) -> None:
    _, _, _, rule = _setup_rule(session, enabled=False)
    registry = _registry_with_fake_store(
        "RetailerA",
        products={
            "fake-123": {
                "name": "Duopack Evoli 30 ans",
                "price": 13.99,
                "available": True,
                "seller": "RetailerA",
                "url": "https://a.example/p/1",
            }
        },
    )

    result = run_check(rule, registry)

    assert result.success is False
    assert "disabled" in result.error
    assert result.observation is None


def test_global_rule_without_listing_is_explicit(session: Session) -> None:
    _, _, _, rule = _setup_rule(session, with_listing=False)
    registry = ConnectorRegistry()

    result = run_check(rule, registry)

    assert result.success is False
    assert "listing" in result.error
    assert result.observation is None


def test_listing_without_external_id_is_explicit(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli 30 ans")
    merchant = crud.create_merchant(session, "RetailerA")
    listing = crud.create_listing(
        session, product_id=product.id, merchant_id=merchant.id, url="https://a.example/p/1"
    )
    rule = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=300, max_quantity=1
    )
    registry = ConnectorRegistry()

    result = run_check(rule, registry)

    assert result.success is False
    assert "external_id" in result.error


def test_observation_is_persisted(session: Session) -> None:
    _, _, listing, rule = _setup_rule(session)
    registry = _registry_with_fake_store(
        "RetailerA",
        products={
            "fake-123": {
                "name": "Duopack Evoli 30 ans",
                "price": 13.99,
                "available": True,
                "seller": "RetailerA",
                "url": "https://a.example/p/1",
            }
        },
    )

    run_check_and_store(session, rule, registry)

    records = crud.list_observation_records_for_listing(session, listing.id)
    assert len(records) == 1
    assert records[0].price == Decimal("13.99")


def test_two_successive_checks_produce_two_records(session: Session) -> None:
    _, _, listing, rule = _setup_rule(session)
    connector = FakeStoreConnector(
        products={
            "fake-123": {
                "name": "Duopack Evoli 30 ans",
                "price": 13.99,
                "available": True,
                "seller": "RetailerA",
                "url": "https://a.example/p/1",
            }
        }
    )
    registry = ConnectorRegistry()
    registry.register("RetailerA", connector)

    run_check_and_store(session, rule, registry)
    connector.update_product("fake-123", price=12.49)
    run_check_and_store(session, rule, registry)

    records = crud.list_observation_records_for_listing(session, listing.id)
    assert len(records) == 2
    assert records[0].price == Decimal("13.99")
    assert records[1].price == Decimal("12.49")


def test_no_observation_persisted_when_connector_fails(session: Session) -> None:
    _, _, listing, rule = _setup_rule(session)
    registry = _registry_with_fake_store(
        "RetailerA",
        errors={"fake-123": ConnectorError("merchant is down")},
    )

    result = run_check_and_store(session, rule, registry)

    assert result.success is False
    records = crud.list_observation_records_for_listing(session, listing.id)
    assert records == []


def test_connector_resolved_via_registry_not_hardcoded(session: Session) -> None:
    product_a, merchant_a, listing_a, rule_a = _setup_rule(
        session, merchant_name="RetailerA", external_id="fake-a"
    )
    product_b = crud.create_product(session, "ETB Pokemon 30 ans")
    merchant_b = crud.create_merchant(session, "RetailerB")
    listing_b = crud.create_listing(
        session,
        product_id=product_b.id,
        merchant_id=merchant_b.id,
        url="https://b.example/p/1",
        external_id="fake-b",
    )
    rule_b = crud.create_watch_rule(
        session,
        product_id=product_b.id,
        listing_id=listing_b.id,
        check_interval=300,
        max_quantity=1,
    )

    registry = ConnectorRegistry()
    registry.register(
        "RetailerA",
        FakeStoreConnector(
            products={
                "fake-a": {
                    "name": "Duopack Evoli 30 ans",
                    "price": 13.99,
                    "available": True,
                    "seller": "RetailerA",
                    "url": "https://a.example/p/1",
                }
            }
        ),
    )
    registry.register(
        "RetailerB",
        FakeStoreConnector(
            products={
                "fake-b": {
                    "name": "ETB Pokemon 30 ans",
                    "price": 59.99,
                    "available": True,
                    "seller": "RetailerB",
                    "url": "https://b.example/p/1",
                }
            }
        ),
    )

    result_a = run_check(rule_a, registry)
    result_b = run_check(rule_b, registry)

    assert result_a.observation.price == Decimal("13.99")
    assert result_b.observation.price == Decimal("59.99")


def test_run_all_active_watch_rules_skips_disabled(session: Session) -> None:
    _, merchant, listing_active, rule_active = _setup_rule(
        session, merchant_name="RetailerA", external_id="fake-active"
    )
    _, _, _, rule_disabled = _setup_rule(
        session, merchant=merchant, external_id="fake-disabled", enabled=False
    )
    registry = _registry_with_fake_store(
        "RetailerA",
        products={
            "fake-active": {
                "name": "Duopack Evoli 30 ans",
                "price": 13.99,
                "available": True,
                "seller": "RetailerA",
                "url": "https://a.example/p/1",
            }
        },
    )

    results = run_all_active_watch_rules(session, registry)

    assert len(results) == 1
    assert results[0].watch_rule_id == rule_active.id
    assert results[0].success is True
