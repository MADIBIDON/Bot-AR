"""Phase 27, section 6: the exact end-to-end adversarial scenarios asked
for, driven through app/worker.py::tick() (the real production entry
point) with `session=` wired so notifications go through the durable
delivery path — not through the crud/app.delivery unit tests in
tests/test_notification_delivery.py, which exercise the same machinery
one layer down.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.worker import _background_tasks, tick
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from database import crud
from database.session import create_all, get_engine
from products.observation import ProductObservation


def _seed_manual_event(session, listing_id: int, *, watch_rule_id: int = 1):
    observation = ProductObservation(
        merchant="RetailerA",
        external_id="fake-123",
        name="Duopack Evoli 30 ans",
        price=Decimal("16.99"),
        currency="EUR",
        available=True,
        url="https://a.example/p/fake-123",
        observed_at=datetime.now(UTC),
    )
    record = crud.create_observation_record(session, listing_id=listing_id, observation=observation)
    event = crud.create_event_record(
        session,
        event_type="stock_available",
        listing_id=listing_id,
        watch_rule_id=watch_rule_id,
        observation_record_id=record.id,
        occurred_at=datetime.now(UTC),
    )
    assert event is not None
    return event


def _setup_rule(
    session,
    *,
    merchant_name: str = "RetailerA",
    external_id: str = "fake-123",
    check_interval: int = 1,
):
    product = crud.create_product(session, "Duopack Evoli 30 ans", ean="1234567890123")
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
        max_price=Decimal("999"),
    )
    return listing, rule


def _fake_product(*, available: bool, price: float = 16.99) -> dict[str, object]:
    return {
        "name": "Duopack Evoli 30 ans",
        "price": price,
        "available": available,
        "seller": "RetailerA",
        "url": "https://a.example/p/fake-123",
        "ean": "1234567890123",
    }


class SuccessNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


class TimeoutThenSuccessNotifier:
    def __init__(self, *, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("simulated Discord timeout")
        self.sent_embeds.append(embed)


async def _drain_background_tasks() -> None:
    while _background_tasks:
        await asyncio.sleep(0)


# --- A: OUT -> IN, Discord success -> exactly one SENT ---------------------


def test_a_out_to_in_discord_success_sends_exactly_once(session) -> None:
    listing, _ = _setup_rule(session)
    registry = ConnectorRegistry()
    connector = FakeStoreConnector(products={"fake-123": _fake_product(available=False)})
    registry.register("RetailerA", connector)
    notifier = SuccessNotifier()
    t0 = datetime.now(UTC)

    async def scenario() -> None:
        await tick(session, registry, notifier, now=t0)
        connector.update_product("fake-123", available=True)
        await tick(session, registry, notifier, now=t0 + timedelta(seconds=2))
        await _drain_background_tasks()

    asyncio.run(scenario())

    assert len(notifier.sent_embeds) == 1
    counts = crud.notification_delivery_status_counts(session)
    assert counts == {"sent": 1}


# --- B: OUT -> IN, Discord timeout -> retry -> SENT, delivered once -------


def test_b_timeout_then_retry_delivers_exactly_once(session) -> None:
    listing, _ = _setup_rule(session)
    registry = ConnectorRegistry()
    connector = FakeStoreConnector(products={"fake-123": _fake_product(available=False)})
    registry.register("RetailerA", connector)
    notifier = TimeoutThenSuccessNotifier(fail_times=1)
    t0 = datetime.now(UTC)

    async def scenario() -> None:
        await tick(session, registry, notifier, now=t0)
        connector.update_product("fake-123", available=True)
        await tick(session, registry, notifier, now=t0 + timedelta(seconds=2))
        await _drain_background_tasks()

    asyncio.run(scenario())

    counts = crud.notification_delivery_status_counts(session)
    assert counts.get("failed_retryable", 0) == 1
    assert notifier.calls == 1
    assert notifier.sent_embeds == []

    from app.delivery import process_due_deliveries

    delivery_row = crud.list_due_deliveries(session, now=t0 + timedelta(hours=1))[0]
    retry_time = delivery_row.next_retry_at + timedelta(seconds=1)
    asyncio.run(process_due_deliveries(session, notifier, now=retry_time))

    counts = crud.notification_delivery_status_counts(session)
    assert counts == {"sent": 1}
    assert notifier.calls == 2
    assert len(notifier.sent_embeds) == 1


# --- C: OUT -> IN, Discord timeout, "crash", restart -> recovered, sent once


def test_c_pending_retry_survives_a_simulated_restart_and_sends_once(tmp_path) -> None:
    """ "Restart" = a brand-new Session bound to the same on-disk SQLite
    file, exactly like app.main_worker.py starting fresh against
    data/app.db — nothing in-memory carries over except the DB itself."""
    db_path = tmp_path / "restart_test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    session_before_crash = session_factory()
    _setup_rule(session_before_crash)
    registry = ConnectorRegistry()
    connector = FakeStoreConnector(products={"fake-123": _fake_product(available=False)})
    registry.register("RetailerA", connector)
    always_timeout = TimeoutThenSuccessNotifier(fail_times=999)
    t0 = datetime.now(UTC)

    async def before_crash() -> None:
        await tick(session_before_crash, registry, always_timeout, now=t0)
        connector.update_product("fake-123", available=True)
        await tick(
            session_before_crash,
            registry,
            always_timeout,
            now=t0 + timedelta(seconds=2),
        )
        await _drain_background_tasks()

    asyncio.run(before_crash())

    counts = crud.notification_delivery_status_counts(session_before_crash)
    assert counts.get("failed_retryable", 0) == 1
    session_before_crash.close()  # simulates the process dying — no clean shutdown logic runs

    # "Restart": fresh session, fresh registry, fresh notifier — only the
    # database file itself survived.
    session_after_restart = session_factory()
    from app.delivery import process_due_deliveries

    recovering_notifier = SuccessNotifier()
    later = t0 + timedelta(hours=1)

    attempted = asyncio.run(
        process_due_deliveries(session_after_restart, recovering_notifier, now=later)
    )

    assert attempted == 1
    assert len(recovering_notifier.sent_embeds) == 1
    counts = crud.notification_delivery_status_counts(session_after_restart)
    assert counts == {"sent": 1}

    # A later sweep must not resend — it's already 'sent'.
    again = asyncio.run(
        process_due_deliveries(
            session_after_restart, recovering_notifier, now=later + timedelta(hours=1)
        )
    )
    assert again == 0
    assert len(recovering_notifier.sent_embeds) == 1


# --- D: Discord success committed, "crash" right after -> no duplicate ----


def test_d_no_duplicate_send_once_success_is_durably_committed(session) -> None:
    """Documents the exact semantics per section 6.D: once mark_delivery_
    sent()'s commit has returned, the row is 'sent' and permanently
    excluded from every future due-delivery query — a crash after that
    point has nothing left to redo. The only unrecoverable-by-construction
    gap is the network/DB boundary itself (Discord received the embed but
    the process died before the local commit) — not simulated here since
    it requires an idempotency key on Discord's side, which the Discord
    message API does not expose to a bot; documented, not silently
    ignored."""
    listing, _ = _setup_rule(session)
    registry = ConnectorRegistry()
    connector = FakeStoreConnector(products={"fake-123": _fake_product(available=False)})
    registry.register("RetailerA", connector)
    notifier = SuccessNotifier()
    t0 = datetime.now(UTC)

    async def scenario() -> None:
        await tick(session, registry, notifier, now=t0)
        connector.update_product("fake-123", available=True)
        await tick(session, registry, notifier, now=t0 + timedelta(seconds=2))
        await _drain_background_tasks()

    asyncio.run(scenario())
    assert len(notifier.sent_embeds) == 1

    from app.delivery import process_due_deliveries

    # Simulated "restart sweep" runs again right after — must be a no-op.
    attempted = asyncio.run(process_due_deliveries(session, notifier, now=t0 + timedelta(hours=1)))

    assert attempted == 0
    assert len(notifier.sent_embeds) == 1


# --- E: two workers cannot process the same delivery concurrently --------


def test_e_two_concurrent_claims_only_one_wins(tmp_path) -> None:
    db_path = tmp_path / "concurrency_test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    seed_session = session_factory()
    listing, rule = _setup_rule(seed_session)
    event = _seed_manual_event(seed_session, listing.id, watch_rule_id=rule.id)
    delivery_row = crud.create_notification_delivery(
        seed_session, event_id=event.id, provider="discord", payload_json="{}"
    )
    seed_session.close()

    results: list[object] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    now = datetime.now(UTC)

    def _race_claim() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)
            claimed = crud.claim_delivery_for_sending(session, delivery_row.id, now=now)
            with lock:
                results.append(claimed)
        finally:
            session.close()

    threads = [threading.Thread(target=_race_claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert len(results) == 2


# --- F: FAILED_PERMANENT visible in healthcheck/status ---------------------


def test_f_permanent_failure_is_visible_in_healthcheck(session) -> None:
    import app.healthcheck as healthcheck

    listing, rule = _setup_rule(session)
    event = _seed_manual_event(session, listing.id, watch_rule_id=rule.id)
    delivery_row = crud.create_notification_delivery(
        session, event_id=event.id, provider="discord", payload_json="{}"
    )
    crud.mark_delivery_failed_permanent(session, delivery_row.id, error_type="forbidden")

    result = healthcheck._check_notifications(session)

    assert "permanent_failed=1" in result.detail
    assert result.blocking_failure is False
