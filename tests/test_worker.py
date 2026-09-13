from __future__ import annotations

import asyncio
import time
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


def _dummy_rule(check_interval: int, scheduled_release_at=None):
    from database.models import WatchRule

    return WatchRule(
        id=1,
        product_id=1,
        check_interval=check_interval,
        max_quantity=1,
        scheduled_release_at=scheduled_release_at,
    )


def test_is_due_scheduled_release_speeds_up_checks_near_the_drop() -> None:
    """Phase 31 section 12: a rule with a real scheduled_release_at checks
    faster than its own base check_interval as the release approaches —
    but never faster than engine.worker.MIN_CHECK_INTERVAL_SECONDS."""
    release_at = datetime(2026, 9, 15, 7, 0, 0, tzinfo=UTC)
    rule = _dummy_rule(check_interval=300, scheduled_release_at=release_at)

    now = release_at - timedelta(seconds=30)
    last_observed = now - timedelta(seconds=35)
    # 35s elapsed < base 300s interval -> would NOT be due without release
    # awareness, but T-30s is inside the high-frequency window (30s floor).
    assert is_due(rule, last_observed, now) is True


def test_is_due_unaffected_when_no_scheduled_release() -> None:
    """A rule with scheduled_release_at=None (every existing WatchRule)
    behaves byte-identical to before this phase."""
    rule = _dummy_rule(check_interval=300)
    now = datetime.now(UTC)
    last_observed = now - timedelta(seconds=35)
    assert is_due(rule, last_observed, now) is False


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


def test_opportunity_score_improved_notifies_without_stock_or_price_change(
    session: Session,
) -> None:
    """Phase 31: editing a rule's manual resale config between two ticks
    (no stock/price change at all — a real scenario, e.g. a human raising
    estimated_resale_price after spotting a hot resale market) must still
    surface as a Discord alert once the score crosses into HIGH/URGENT —
    section 7/11's "score crossed an important threshold" trigger."""
    _, _, _, rule = _setup_rule(session, external_id="fake-123", check_interval=1)
    connector = FakeStoreConnector(products={"fake-123": _fake_product(price=20.0)})
    registry = ConnectorRegistry()
    registry.register("RetailerA", connector)
    notifier = FakeNotifier()

    t0 = datetime.now(UTC)
    asyncio.run(tick(session, registry, notifier, now=t0))  # baseline, no opportunity configured
    assert notifier.sent_embeds == []

    crud.update_watch_rule(
        session,
        rule.id,
        estimated_resale_price=Decimal("200"),
        estimated_resale_trusted=True,
    )
    # Same price, same stock -> zero MonitoringEvents this tick, yet the
    # opportunity score just became excellent.
    asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=2)))

    assert len(notifier.sent_embeds) == 1
    refreshed = crud.get_watch_rule(session, rule.id)
    assert refreshed.last_alert_tier in ("high", "urgent")

    # A third, unchanged tick must not re-notify for the same tier.
    asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=4)))
    assert len(notifier.sent_embeds) == 1


def test_opportunity_shift_bug_never_blocks_other_rules_monitoring(
    session: Session, monkeypatch
) -> None:
    """Phase 31 section 15: a genuine bug in the new, optional opportunity-
    shift path (here: evaluate_opportunity_intelligence raising) must never
    stop the rest of tick()'s for-loop — a *later* WatchRule in the same
    tick with a real stock event still gets checked and still notified.
    The "quiet" rule is created first (and so processed first, same insert
    order tick() iterates in) specifically so its exception has a chance
    to propagate past it if the try/except around it were ever removed."""
    merchant = crud.create_merchant(session, "RetailerA")
    _setup_rule(session, merchant=merchant, external_id="quiet-1", check_interval=1)
    _setup_rule(
        session,
        merchant=merchant,
        external_id="restocks-1",
        check_interval=1,
    )
    registry = ConnectorRegistry()
    registry.register(
        "RetailerA",
        FakeStoreConnector(
            products={
                "quiet-1": _fake_product(url="https://a.example/p/quiet-1"),
                "restocks-1": _fake_product(
                    url="https://a.example/p/restocks-1", price=16.99, available=False
                ),
            }
        ),
    )
    notifier = FakeNotifier()

    import app.worker as worker_module

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "evaluate_opportunity_intelligence", _boom)

    t0 = datetime.now(UTC)
    asyncio.run(tick(session, registry, notifier, now=t0))  # baseline for both

    connector = registry.get("RetailerA")
    connector.update_product("restocks-1", available=True)  # real restock event, unrelated rule
    results = asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=2)))

    assert all(r.success for r in results)
    assert len(notifier.sent_embeds) == 1  # the real restock still notified normally


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
    # Backoff is keyed by merchant (Phase 26, section 11), not rule.id.
    assert backoff.is_blocked("merchant:RetailerA", t0 + timedelta(seconds=1)) is True

    # Swap in a working connector and let the backoff window pass.
    registry.register("RetailerA", FakeStoreConnector(products={"fake-123": _fake_product()}))
    later = t0 + timedelta(seconds=150)
    results = asyncio.run(run_monitoring_tick(session, registry, now=later, backoff=backoff))

    assert len(results) == 1
    assert results[0][1].success is True
    assert backoff.is_blocked("merchant:RetailerA", later) is False


def test_429_on_one_merchant_rule_blocks_sibling_rules_same_merchant_only(
    session: Session,
) -> None:
    """Phase 26 audit, section 11: three rules on the same merchant
    (Kairyu A/B/C) plus one rule on a different merchant (Relic D). A
    429 anywhere on Kairyu must stop B and C from hammering it too on
    the very next tick — but must never touch Relic."""
    kairyu = crud.create_merchant(session, "Kairyu")
    _, _, listing_a, rule_a = _setup_rule(
        session, merchant=kairyu, external_id="kairyu-a", check_interval=1
    )
    _, _, listing_b, rule_b = _setup_rule(
        session, merchant=kairyu, external_id="kairyu-b", check_interval=1
    )
    _, _, listing_c, rule_c = _setup_rule(
        session, merchant=kairyu, external_id="kairyu-c", check_interval=1
    )
    _, _, listing_d, rule_d = _setup_rule(
        session, merchant_name="Relic", external_id="relic-d", check_interval=1
    )

    registry = ConnectorRegistry()
    registry.register(
        "Kairyu",
        FakeStoreConnector(
            products={
                "kairyu-b": _fake_product(seller="Kairyu"),
                "kairyu-c": _fake_product(seller="Kairyu"),
            },
            errors={"kairyu-a": ConnectorError("rate limited (429) fetching kairyu-a")},
        ),
    )
    registry.register(
        "Relic", FakeStoreConnector(products={"relic-d": _fake_product(seller="Relic")})
    )

    backoff = BackoffTracker(base_seconds=100, max_seconds=1000)
    t0 = datetime.now(UTC)

    first = asyncio.run(run_monitoring_tick(session, registry, now=t0, backoff=backoff))
    assert {rule.id for rule, _ in first} == {rule_a.id, rule_b.id, rule_c.id, rule_d.id}
    outcomes = {rule.id: result.success for rule, result in first}
    assert outcomes[rule_a.id] is False  # the 429
    assert outcomes[rule_b.id] is True
    assert outcomes[rule_c.id] is True
    assert outcomes[rule_d.id] is True

    # Next tick: B and C must now be skipped too (same merchant as the
    # 429), Relic's D must still be checked normally.
    second = asyncio.run(
        run_monitoring_tick(session, registry, now=t0 + timedelta(seconds=5), backoff=backoff)
    )
    checked_ids = {rule.id for rule, _ in second}
    assert rule_a.id not in checked_ids
    assert rule_b.id not in checked_ids
    assert rule_c.id not in checked_ids
    assert rule_d.id in checked_ids


# --- Phase 26: Discord must never block the fast path ----------------------


class SlowNotifier:
    """send_embed() sleeps — used to prove a slow/hung Discord call can't
    stall tick() or the next tick's monitoring of other WatchRules."""

    def __init__(self, *, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        await asyncio.sleep(self._sleep_seconds)
        self.sent_embeds.append(embed)


async def _drain_background_tasks() -> None:
    from app.worker import _background_tasks

    while _background_tasks:
        await asyncio.sleep(0)


def test_tick_returns_without_waiting_for_slow_discord_send(session: Session) -> None:
    _, _, _, rule = _setup_rule(
        session,
        product_ean="1234567890123",
        external_id="fake-123",
        check_interval=1,
        max_price=Decimal("19.99"),
    )
    connector = FakeStoreConnector(
        products={"fake-123": _fake_product(price=16.99, ean="1234567890123")}
    )
    registry = ConnectorRegistry()
    registry.register("RetailerA", connector)
    notifier = SlowNotifier(sleep_seconds=1.5)

    async def scenario() -> float:
        t0 = datetime.now(UTC)
        await tick(session, registry, notifier, now=t0)  # baseline, no event
        connector.update_product("fake-123", price=12.49)  # plain price drop -> one event

        start = time.monotonic()
        await tick(session, registry, notifier, now=t0 + timedelta(seconds=2))
        elapsed = time.monotonic() - start
        await _drain_background_tasks()
        return elapsed

    elapsed = asyncio.run(scenario())

    assert elapsed < 1.0  # tick() returned well before the notifier's 1.5s sleep finished
    assert len(notifier.sent_embeds) == 1  # the alert was still actually delivered


def test_other_watch_rule_still_monitored_promptly_while_discord_is_slow(
    session: Session,
) -> None:
    """The exact scenario from the audit: a slow Discord send for one
    WatchRule's alert must never delay another WatchRule's own monitoring
    check on the very next tick."""
    _, _, _, slow_alert_rule = _setup_rule(
        session,
        merchant_name="RetailerA",
        product_ean="1234567890123",
        external_id="fake-slow",
        check_interval=1,
        max_price=Decimal("100"),
    )
    _, _, _, other_rule = _setup_rule(
        session,
        merchant_name="RetailerB",
        external_id="fake-other",
        check_interval=1,
    )
    registry = ConnectorRegistry()
    connector_a = FakeStoreConnector(
        products={
            "fake-slow": _fake_product(
                price=16.99, ean="1234567890123", url="https://a.example/p/fake-slow"
            )
        }
    )
    connector_b = FakeStoreConnector(
        products={"fake-other": _fake_product(url="https://b.example/p/fake-other")}
    )
    registry.register("RetailerA", connector_a)
    registry.register("RetailerB", connector_b)
    notifier = SlowNotifier(sleep_seconds=1.5)

    async def scenario() -> float:
        t0 = datetime.now(UTC)
        await tick(session, registry, notifier, now=t0)  # baseline for both, no events
        connector_a.update_product("fake-slow", price=9.99)  # triggers an event -> slow send

        start = time.monotonic()
        results = await tick(session, registry, notifier, now=t0 + timedelta(seconds=2))
        elapsed = time.monotonic() - start
        await _drain_background_tasks()
        assert {r.watch_rule_id for r in results} == {slow_alert_rule.id, other_rule.id}
        assert all(r.success for r in results)
        return elapsed

    elapsed = asyncio.run(scenario())

    assert elapsed < 1.0


# --- Phase 26 audit, section 4: restock dedup + crash-recovery ------------


class AlwaysFailingNotifier:
    def __init__(self) -> None:
        self.calls = 0
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.calls += 1
        raise RuntimeError("simulated Discord outage")


def test_restock_dedup_sequence_out_out_in_in_out_in(session: Session) -> None:
    """Phase 26 audit, section 4's exact sequence: OUT->OUT (no alert),
    OUT->IN (one alert), IN->IN (no spam), IN->OUT (state recorded, no
    alert), OUT->IN (a new alert)."""
    _, _, listing, rule = _setup_rule(
        session, product_ean="1234567890123", external_id="fake-123", check_interval=1
    )
    connector = FakeStoreConnector(
        products={"fake-123": _fake_product(available=False, ean="1234567890123")}
    )
    registry = ConnectorRegistry()
    registry.register("RetailerA", connector)
    notifier = FakeNotifier()
    t0 = datetime.now(UTC)

    asyncio.run(tick(session, registry, notifier, now=t0))  # OUT (baseline) -> no event
    assert notifier.sent_embeds == []

    asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=2)))  # still OUT
    assert notifier.sent_embeds == []

    connector.update_product("fake-123", available=True)
    asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=4)))  # OUT->IN
    assert len(notifier.sent_embeds) == 1

    asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=6)))  # still IN
    assert len(notifier.sent_embeds) == 1  # no spam

    connector.update_product("fake-123", available=False)
    asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=8)))  # IN->OUT
    assert len(notifier.sent_embeds) == 1  # no alert for going out of stock

    connector.update_product("fake-123", available=True)
    asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=10)))  # OUT->IN
    assert len(notifier.sent_embeds) == 2  # a genuinely new alert

    events = crud.list_event_records_for_listing(session, listing.id)
    assert sum(1 for e in events if e.event_type == "stock_available") == 2


def test_failed_notification_then_crash_and_restart_neither_loses_nor_spams(
    session: Session,
) -> None:
    """Phase 26 audit, section 4: OUT->IN, the notification fails
    (simulated Discord outage), then the "worker restarts" (modeled as a
    fresh BackoffTracker plus the next scheduled tick — this project's
    only per-run state, since everything else lives in the DB). The
    restock must not vanish (the EventRecord is durably persisted before
    notification is even attempted — see app/worker.py::tick()'s module
    docstring) and it must not be re-alerted 10 times on every later
    tick either."""
    _, _, listing, rule = _setup_rule(
        session, product_ean="1234567890123", external_id="fake-123", check_interval=1
    )
    connector = FakeStoreConnector(
        products={"fake-123": _fake_product(available=False, ean="1234567890123")}
    )
    registry = ConnectorRegistry()
    registry.register("RetailerA", connector)
    failing_notifier = AlwaysFailingNotifier()
    t0 = datetime.now(UTC)

    asyncio.run(tick(session, registry, failing_notifier, now=t0))  # OUT baseline

    connector.update_product("fake-123", available=True)

    async def restock_tick_with_failing_discord() -> None:
        await tick(session, registry, failing_notifier, now=t0 + timedelta(seconds=2))
        await _drain_background_tasks()  # let the failed background send finish failing

    asyncio.run(restock_tick_with_failing_discord())

    # The event is durably recorded even though Discord never got it —
    # "no permanent zero information" even after a lost notification.
    events = crud.list_event_records_for_listing(session, listing.id)
    restock_events = [e for e in events if e.event_type == "stock_available"]
    assert len(restock_events) == 1

    # "Worker restart": fresh BackoffTracker (the only in-memory state a
    # real restart would clear), same still-in-stock product, several
    # more ticks. Must not re-fire the same restock over and over.
    fresh_notifier = FakeNotifier()
    fresh_backoff = BackoffTracker()
    for i in range(3, 8):
        asyncio.run(
            tick(
                session,
                registry,
                fresh_notifier,
                now=t0 + timedelta(seconds=2 * i),
                backoff=fresh_backoff,
            )
        )

    assert fresh_notifier.sent_embeds == []  # no re-alert: still just IN, unchanged
    events_after_restart = crud.list_event_records_for_listing(session, listing.id)
    restock_events_after = [e for e in events_after_restart if e.event_type == "stock_available"]
    assert len(restock_events_after) == 1  # not duplicated, not multiplied into "ten alerts"
