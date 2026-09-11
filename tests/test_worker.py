from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.worker import DEFAULT_POLL_INTERVAL_SECONDS, run_forever, tick
from connectors.base import ConnectorError
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from database import crud
from engine.backoff import BackoffTracker
from engine.worker import is_due, jitter_seconds, run_monitoring_tick


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


def _setup_rule(
    session: Session,
    *,
    merchant_name: str = "RetailerA",
    merchant=None,
    product_ean: str | None = None,
    external_id: str = "fake-123",
    check_interval: int = 300,
    target_price: Decimal | None = None,
    max_price: Decimal | None = None,
):
    product = crud.create_product(session, "Duopack Evoli 30 ans", ean=product_ean)
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
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=check_interval,
        max_quantity=1,
        target_price=target_price,
        max_price=max_price,
    )
    return product, merchant, listing, rule


def _fake_product(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "name": "Duopack Evoli 30 ans",
        "price": 13.99,
        "available": True,
        "seller": "RetailerA",
        "url": "https://a.example/p/fake-123",
    }
    defaults.update(overrides)
    return defaults


# --- engine.worker: pure scheduling ---------------------------------------


def test_is_due_when_never_observed() -> None:
    assert is_due(_dummy_rule(check_interval=3600), None, datetime.now(UTC)) is True


def test_is_due_when_interval_elapsed() -> None:
    # +30s margin, comfortably past the max possible jitter (Phase 13:
    # is_due adds a small deterministic per-rule jitter, at most 5s or 10%
    # of the interval) so this stays a "clearly elapsed" check rather than
    # an exact-boundary one (see tests/test_worker.py jitter tests for that).
    now = datetime.now(UTC)
    last = now - timedelta(seconds=330)
    assert is_due(_dummy_rule(check_interval=300), last, now) is True


def test_not_due_when_interval_not_elapsed() -> None:
    now = datetime.now(UTC)
    last = now - timedelta(seconds=10)
    assert is_due(_dummy_rule(check_interval=3600), last, now) is False


def _dummy_rule(check_interval: int):
    from database.models import WatchRule

    return WatchRule(id=1, product_id=1, check_interval=check_interval, max_quantity=1)


def test_rule_not_due_is_skipped(session: Session) -> None:
    _, _, listing, rule = _setup_rule(session, check_interval=3600, external_id="fake-123")
    registry = ConnectorRegistry()
    registry.register("RetailerA", FakeStoreConnector(products={"fake-123": _fake_product()}))

    t0 = datetime.now(UTC)
    # first run: always due, creates baseline
    asyncio.run(run_monitoring_tick(session, registry, now=t0))
    assert len(crud.list_observation_records_for_listing(session, listing.id)) == 1

    # second tick, only 10s later: check_interval=3600 -> must NOT run again
    pairs = asyncio.run(run_monitoring_tick(session, registry, now=t0 + timedelta(seconds=10)))
    assert pairs == []
    assert len(crud.list_observation_records_for_listing(session, listing.id)) == 1


def test_rule_due_is_checked(session: Session) -> None:
    _, _, listing, rule = _setup_rule(session, check_interval=1, external_id="fake-123")
    registry = ConnectorRegistry()
    registry.register("RetailerA", FakeStoreConnector(products={"fake-123": _fake_product()}))

    pairs = asyncio.run(run_monitoring_tick(session, registry))

    assert len(pairs) == 1
    assert pairs[0][0].id == rule.id
    assert pairs[0][1].success is True


def test_one_failing_rule_does_not_block_the_other(session: Session) -> None:
    merchant = crud.create_merchant(session, "RetailerA")
    _, _, listing_ok, rule_ok = _setup_rule(
        session, merchant=merchant, external_id="fake-ok", check_interval=1
    )
    _, _, listing_bad, rule_bad = _setup_rule(
        session, merchant=merchant, external_id="fake-missing", check_interval=1
    )
    registry = ConnectorRegistry()
    registry.register(
        "RetailerA",
        FakeStoreConnector(products={"fake-ok": _fake_product(url="https://a.example/p/fake-ok")}),
    )

    pairs = asyncio.run(run_monitoring_tick(session, registry))

    results_by_rule = {rule.id: result for rule, result in pairs}
    assert results_by_rule[rule_ok.id].success is True
    assert results_by_rule[rule_bad.id].success is False


def test_one_merchant_failing_does_not_block_the_other_two(session: Session) -> None:
    """Phase 14: three real-shaped merchants in the same registry — a
    connector error on one must never stop the other two from being
    checked in the same tick."""
    merchant_a, _, listing_a, rule_a = _setup_rule(
        session, merchant_name="MerchantA", external_id="a-1", check_interval=1
    )
    _, _, listing_b, rule_b = _setup_rule(
        session, merchant_name="MerchantB", external_id="b-1", check_interval=1
    )
    _, _, listing_c, rule_c = _setup_rule(
        session, merchant_name="MerchantC", external_id="c-1", check_interval=1
    )

    registry = ConnectorRegistry()
    registry.register(
        "MerchantA", FakeStoreConnector(products={"a-1": _fake_product(url="https://a.example/1")})
    )
    registry.register(
        "MerchantB", FakeStoreConnector(errors={"b-1": ConnectorError("HTTP 503 fetching x")})
    )
    registry.register(
        "MerchantC", FakeStoreConnector(products={"c-1": _fake_product(url="https://c.example/1")})
    )

    pairs = asyncio.run(run_monitoring_tick(session, registry))
    results_by_rule = {rule.id: result for rule, result in pairs}

    assert results_by_rule[rule_a.id].success is True
    assert results_by_rule[rule_b.id].success is False
    assert results_by_rule[rule_c.id].success is True


# --- app.worker: tick() adds decision + notification ----------------------


def test_no_event_on_first_tick_sends_no_notification(session: Session) -> None:
    _setup_rule(session, external_id="fake-123", check_interval=1)
    registry = ConnectorRegistry()
    registry.register("RetailerA", FakeStoreConnector(products={"fake-123": _fake_product()}))
    notifier = FakeNotifier()

    asyncio.run(tick(session, registry, notifier))

    assert notifier.sent_embeds == []


def test_event_with_allowed_decision_sends_notification(session: Session) -> None:
    _, _, _, rule = _setup_rule(
        session,
        product_ean="1234567890123",
        external_id="fake-123",
        check_interval=1,
        target_price=Decimal("14.99"),
        max_price=Decimal("19.99"),
    )
    connector = FakeStoreConnector(
        products={"fake-123": _fake_product(price=16.99, ean="1234567890123")}
    )
    registry = ConnectorRegistry()
    registry.register("RetailerA", connector)
    notifier = FakeNotifier()

    t0 = datetime.now(UTC)
    asyncio.run(tick(session, registry, notifier, now=t0))  # baseline, no event

    connector.update_product("fake-123", price=12.49)  # drops below target
    asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=2)))

    assert len(notifier.sent_embeds) >= 1


def test_event_with_rejected_decision_sends_no_notification(session: Session) -> None:
    _, _, _, rule = _setup_rule(
        session,
        product_ean="1234567890123",
        external_id="fake-123",
        check_interval=1,
        max_price=Decimal("5"),  # even the baseline price exceeds this
    )
    connector = FakeStoreConnector(
        products={"fake-123": _fake_product(price=16.99, ean="1234567890123")}
    )
    registry = ConnectorRegistry()
    registry.register("RetailerA", connector)
    notifier = FakeNotifier()

    t0 = datetime.now(UTC)
    asyncio.run(tick(session, registry, notifier, now=t0))  # baseline, no event yet

    connector.update_product("fake-123", price=12.49)  # price drop event, but still > max_price
    asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=2)))

    assert notifier.sent_embeds == []


def test_connector_error_produces_no_notification(session: Session) -> None:
    _setup_rule(session, external_id="fake-123", check_interval=1)
    registry = ConnectorRegistry()
    registry.register(
        "RetailerA",
        FakeStoreConnector(errors={"fake-123": ConnectorError("merchant is down")}),
    )
    notifier = FakeNotifier()

    results = asyncio.run(tick(session, registry, notifier))

    assert results[0].success is False
    assert notifier.sent_embeds == []


# --- run_forever: clean shutdown -------------------------------------------


def test_run_forever_stops_cleanly_without_real_wait(session: Session) -> None:
    registry = ConnectorRegistry()
    notifier = FakeNotifier()
    stop_event = asyncio.Event()

    async def stopper() -> None:
        await asyncio.sleep(0)
        stop_event.set()

    async def run() -> None:
        await asyncio.gather(
            run_forever(session, registry, notifier, poll_interval=999, stop_event=stop_event),
            stopper(),
        )

    asyncio.run(asyncio.wait_for(run(), timeout=5))

    assert stop_event.is_set()


def test_default_poll_interval_is_reasonable() -> None:
    assert 10 <= DEFAULT_POLL_INTERVAL_SECONDS <= 30


# --- jitter ------------------------------------------------------------


def test_jitter_is_deterministic() -> None:
    assert jitter_seconds(42, 300) == jitter_seconds(42, 300)


def test_jitter_is_bounded() -> None:
    for rule_id in range(20):
        j = jitter_seconds(rule_id, 300)
        assert 0 <= j <= 5.0


def test_jitter_scales_down_for_short_intervals() -> None:
    # interval=10 -> max jitter is 10% of 10 = 1.0s, not the 5s ceiling
    for rule_id in range(20):
        assert jitter_seconds(rule_id, 10) <= 1.0


def test_jitter_differs_across_rule_ids() -> None:
    values = {jitter_seconds(rule_id, 300) for rule_id in range(10)}
    assert len(values) > 1  # not all identical


def test_is_due_respects_computed_jitter() -> None:
    rule = _dummy_rule(check_interval=300)
    rule.id = 7
    j = jitter_seconds(7, 300)
    now = datetime.now(UTC)

    just_under = now - timedelta(seconds=300 + j - 1)
    just_over = now - timedelta(seconds=300 + j + 1)

    assert is_due(rule, just_under, now) is False
    assert is_due(rule, just_over, now) is True


# --- backoff integration in run_monitoring_tick -------------------------


def test_backoff_skips_rule_after_retryable_failure(session: Session) -> None:
    _, _, listing, rule = _setup_rule(session, external_id="fake-123", check_interval=1)
    registry = ConnectorRegistry()
    registry.register(
        "RetailerA",
        FakeStoreConnector(errors={"fake-123": ConnectorError("rate limited (429) fetching x")}),
    )
    backoff = BackoffTracker(base_seconds=100, max_seconds=1000)
    t0 = datetime.now(UTC)

    first = asyncio.run(run_monitoring_tick(session, registry, now=t0, backoff=backoff))
    assert len(first) == 1
    assert first[0][1].success is False

    # Immediately due again by check_interval (1s and no observation was
    # ever persisted, since the check failed) but backed off for ~100s.
    second = asyncio.run(
        run_monitoring_tick(session, registry, now=t0 + timedelta(seconds=5), backoff=backoff)
    )
    assert second == []


def test_backoff_resets_after_success(session: Session) -> None:
    _, _, listing, rule = _setup_rule(session, external_id="fake-123", check_interval=1)
    connector = FakeStoreConnector(
        errors={"fake-123": ConnectorError("timeout fetching x")},
    )
    registry = ConnectorRegistry()
    registry.register("RetailerA", connector)
    backoff = BackoffTracker(base_seconds=100, max_seconds=1000)
    t0 = datetime.now(UTC)

    asyncio.run(run_monitoring_tick(session, registry, now=t0, backoff=backoff))
    assert backoff.is_blocked(rule.id, t0 + timedelta(seconds=1)) is True

    # Swap in a working connector and let the backoff window pass.
    registry.register("RetailerA", FakeStoreConnector(products={"fake-123": _fake_product()}))
    later = t0 + timedelta(seconds=150)
    results = asyncio.run(run_monitoring_tick(session, registry, now=later, backoff=backoff))

    assert len(results) == 1
    assert results[0][1].success is True
    assert backoff.is_blocked(rule.id, later) is False
