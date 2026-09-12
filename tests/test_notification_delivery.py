"""Phase 27: durable, at-least-once, idempotent Discord notification
delivery. Covers database/crud.py's NotificationDelivery functions and
app/delivery.py's orchestration directly — no real Discord, no real
network; FakeNotifier variants stand in for discord.py's client exactly
like the rest of this project's test suite.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import discord
import pytest
from sqlalchemy.orm import Session

from app import delivery
from database import crud
from database.time_utils import ensure_utc


def _seed_event(session: Session, *, external_id: str = "fake-123") -> tuple[int, int]:
    """Returns (event_record_id, listing_id) for one persisted restock
    event, wired through the same tables app/delivery.py reads. Merchant
    name and product ean are derived from external_id so multiple calls
    in one test never collide on either's UNIQUE constraint."""
    merchant_name = f"RetailerA-{external_id}"
    product = crud.create_product(session, "Duopack Evoli 30 ans")
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
    from products.observation import ProductObservation

    obs1 = ProductObservation(
        merchant=merchant_name,
        external_id=external_id,
        name="Duopack Evoli 30 ans",
        price=Decimal("16.99"),
        currency="EUR",
        available=False,
        url=f"https://a.example/p/{external_id}",
        observed_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    obs2 = ProductObservation(
        merchant=merchant_name,
        external_id=external_id,
        name="Duopack Evoli 30 ans",
        price=Decimal("16.99"),
        currency="EUR",
        available=True,
        url=f"https://a.example/p/{external_id}",
        observed_at=datetime.now(UTC),
    )
    crud.create_observation_record(session, listing_id=listing.id, observation=obs1)
    record2 = crud.create_observation_record(session, listing_id=listing.id, observation=obs2)

    event = crud.create_event_record(
        session,
        event_type="stock_available",
        listing_id=listing.id,
        watch_rule_id=rule.id,
        observation_record_id=record2.id,
        occurred_at=obs2.observed_at,
        previous_value="False",
        current_value="True",
    )
    assert event is not None
    return event.id, listing.id


def _embed(title: str = "Restock") -> discord.Embed:
    embed = discord.Embed(title=title)
    embed.add_field(name="Product", value="Duopack Evoli 30 ans")
    return embed


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[discord.Embed] = []

    async def send_embed(self, embed: discord.Embed) -> None:
        self.sent_embeds.append(embed)


class AlwaysTimeoutNotifier:
    def __init__(self) -> None:
        self.calls = 0

    async def send_embed(self, embed: discord.Embed) -> None:
        self.calls += 1
        raise TimeoutError("simulated Discord timeout")


class FailNTimesThenSucceedNotifier:
    def __init__(self, *, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.sent_embeds: list[discord.Embed] = []

    async def send_embed(self, embed: discord.Embed) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("simulated transient Discord outage")
        self.sent_embeds.append(embed)


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "test"


def _http_exception(status: int) -> discord.HTTPException:
    return discord.HTTPException(_FakeResponse(status), "test error")


# --- crud: NotificationDelivery -------------------------------------------


def test_create_notification_delivery_is_idempotent(session: Session) -> None:
    event_id, _ = _seed_event(session)

    first = crud.create_notification_delivery(
        session, event_id=event_id, provider="discord", payload_json="{}"
    )
    second = crud.create_notification_delivery(
        session, event_id=event_id, provider="discord", payload_json='{"different": true}'
    )

    assert first.id == second.id
    assert second.payload_json == "{}"  # the original payload wins, never overwritten


def test_claim_delivery_for_sending_transitions_pending_to_sending(session: Session) -> None:
    event_id, _ = _seed_event(session)
    delivery_row = crud.create_notification_delivery(
        session, event_id=event_id, provider="discord", payload_json="{}"
    )
    now = datetime.now(UTC)

    claimed = crud.claim_delivery_for_sending(session, delivery_row.id, now=now)

    assert claimed is not None
    assert claimed.status == "sending"
    assert claimed.attempt_count == 1
    assert ensure_utc(claimed.last_attempt_at) == now


def test_claim_delivery_for_sending_refuses_an_already_sent_row(session: Session) -> None:
    event_id, _ = _seed_event(session)
    delivery_row = crud.create_notification_delivery(
        session, event_id=event_id, provider="discord", payload_json="{}"
    )
    now = datetime.now(UTC)
    crud.mark_delivery_sent(session, delivery_row.id, now=now)

    claimed = crud.claim_delivery_for_sending(session, delivery_row.id, now=now)

    assert claimed is None


def test_claim_delivery_for_sending_respects_next_retry_at(session: Session) -> None:
    event_id, _ = _seed_event(session)
    delivery_row = crud.create_notification_delivery(
        session, event_id=event_id, provider="discord", payload_json="{}"
    )
    now = datetime.now(UTC)
    crud.mark_delivery_failed_retryable(
        session, delivery_row.id, error_type="timeout", next_retry_at=now + timedelta(seconds=60)
    )

    too_early = crud.claim_delivery_for_sending(
        session, delivery_row.id, now=now + timedelta(seconds=1)
    )
    assert too_early is None

    on_time = crud.claim_delivery_for_sending(
        session, delivery_row.id, now=now + timedelta(seconds=61)
    )
    assert on_time is not None
    assert on_time.status == "sending"


def test_list_due_deliveries_excludes_sent_and_permanent(session: Session) -> None:
    id_a, _ = _seed_event(session, external_id="a")
    id_b, _ = _seed_event(session, external_id="b")
    id_c, _ = _seed_event(session, external_id="c")
    now = datetime.now(UTC)

    pending = crud.create_notification_delivery(
        session, event_id=id_a, provider="discord", payload_json="{}"
    )
    sent = crud.create_notification_delivery(
        session, event_id=id_b, provider="discord", payload_json="{}"
    )
    crud.mark_delivery_sent(session, sent.id, now=now)
    permanent = crud.create_notification_delivery(
        session, event_id=id_c, provider="discord", payload_json="{}"
    )
    crud.mark_delivery_failed_permanent(session, permanent.id, error_type="forbidden")

    due = crud.list_due_deliveries(session, now=now)

    assert [d.id for d in due] == [pending.id]


def test_notification_delivery_status_counts(session: Session) -> None:
    id_a, _ = _seed_event(session, external_id="a")
    id_b, _ = _seed_event(session, external_id="b")
    now = datetime.now(UTC)
    d1 = crud.create_notification_delivery(
        session, event_id=id_a, provider="discord", payload_json="{}"
    )
    d2 = crud.create_notification_delivery(
        session, event_id=id_b, provider="discord", payload_json="{}"
    )
    crud.mark_delivery_sent(session, d1.id, now=now)
    crud.mark_delivery_failed_permanent(session, d2.id, error_type="forbidden")

    counts = crud.notification_delivery_status_counts(session)

    assert counts == {"sent": 1, "failed_permanent": 1}
    assert ensure_utc(crud.get_last_successful_delivery_at(session)) == now


def test_list_event_records_missing_delivery(session: Session) -> None:
    event_id, _ = _seed_event(session)

    missing = crud.list_event_records_missing_delivery(session)
    assert [e.id for e in missing] == [event_id]

    crud.create_notification_delivery(
        session, event_id=event_id, provider="discord", payload_json="{}"
    )
    assert crud.list_event_records_missing_delivery(session) == []


# --- app/delivery.py: error classification ---------------------------------


@pytest.mark.parametrize(
    ("exc", "expected_type", "expected_retryable"),
    [
        (TimeoutError("x"), "timeout", True),
        (discord.NotFound(_FakeResponse(404), "x"), "not_found", False),
        (discord.Forbidden(_FakeResponse(403), "x"), "forbidden", False),
        (discord.RateLimited(2.5), "rate_limited", True),
        (discord.DiscordServerError(_FakeResponse(503), "x"), "discord_5xx", True),
        (_http_exception(429), "rate_limited", True),
        (_http_exception(500), "discord_5xx", True),
        (_http_exception(400), "http_error", False),
        (ConnectionError("x"), "network_error", True),
        (RuntimeError("something odd"), "unknown", True),
    ],
)
def test_classify_send_error(
    exc: BaseException, expected_type: str, expected_retryable: bool
) -> None:
    error_type, retryable = delivery.classify_send_error(exc)

    assert error_type == expected_type
    assert retryable is expected_retryable


# --- app/delivery.py: attempt_delivery / process_due_deliveries -----------


def test_attempt_delivery_success_marks_sent_and_sends_once(session: Session) -> None:
    event_id, _ = _seed_event(session)
    delivery_row = delivery.create_pending_delivery(session, event_id=event_id, embed=_embed())
    notifier = RecordingNotifier()

    asyncio.run(delivery.attempt_delivery(session, delivery_row.id, notifier))

    refreshed = crud.get_notification_delivery(session, event_id=event_id, provider="discord")
    assert refreshed.status == "sent"
    assert len(notifier.sent_embeds) == 1


def test_attempt_delivery_retryable_failure_schedules_a_retry(session: Session) -> None:
    event_id, _ = _seed_event(session)
    delivery_row = delivery.create_pending_delivery(session, event_id=event_id, embed=_embed())
    notifier = AlwaysTimeoutNotifier()
    now = datetime.now(UTC)

    asyncio.run(delivery.attempt_delivery(session, delivery_row.id, notifier, now=now))

    refreshed = crud.get_notification_delivery(session, event_id=event_id, provider="discord")
    assert refreshed.status == "failed_retryable"
    assert refreshed.last_error_type == "timeout"
    assert refreshed.next_retry_at is not None
    assert refreshed.next_retry_at > now


def test_attempt_delivery_permanent_error_never_retries(session: Session) -> None:
    event_id, _ = _seed_event(session)
    delivery_row = delivery.create_pending_delivery(session, event_id=event_id, embed=_embed())

    class ForbiddenNotifier:
        async def send_embed(self, embed: discord.Embed) -> None:
            raise discord.Forbidden(_FakeResponse(403), "missing permission")

    asyncio.run(delivery.attempt_delivery(session, delivery_row.id, ForbiddenNotifier()))

    refreshed = crud.get_notification_delivery(session, event_id=event_id, provider="discord")
    assert refreshed.status == "failed_permanent"
    assert refreshed.last_error_type == "forbidden"
    assert refreshed.next_retry_at is None


def test_exhausting_max_attempts_gives_up_even_on_a_retryable_error(session: Session) -> None:
    """No infinite retry loop: a permanently-timing-out Discord eventually
    becomes visible as failed_permanent instead of retrying forever."""
    event_id, _ = _seed_event(session)
    delivery_row = delivery.create_pending_delivery(session, event_id=event_id, embed=_embed())
    notifier = AlwaysTimeoutNotifier()
    now = datetime.now(UTC)

    for attempt in range(delivery._MAX_ATTEMPTS):
        due_now = crud.list_due_deliveries(session, now=now + timedelta(hours=attempt + 1))
        assert len(due_now) == 1
        asyncio.run(
            delivery.attempt_delivery(
                session, delivery_row.id, notifier, now=now + timedelta(hours=attempt + 1)
            )
        )

    refreshed = crud.get_notification_delivery(session, event_id=event_id, provider="discord")
    assert refreshed.status == "failed_permanent"
    assert notifier.calls == delivery._MAX_ATTEMPTS


def test_process_due_deliveries_retries_then_delivers(session: Session) -> None:
    """Section 6.B: OUT->IN, Discord timeout, retry, eventually SENT —
    exactly one delivered notification."""
    event_id, _ = _seed_event(session)
    delivery.create_pending_delivery(session, event_id=event_id, embed=_embed())
    notifier = FailNTimesThenSucceedNotifier(fail_times=2)
    now = datetime.now(UTC)

    attempted_first = asyncio.run(delivery.process_due_deliveries(session, notifier, now=now))
    assert attempted_first == 1
    refreshed = crud.get_notification_delivery(session, event_id=event_id, provider="discord")
    assert refreshed.status == "failed_retryable"

    later = refreshed.next_retry_at + timedelta(seconds=1)
    attempted_second = asyncio.run(delivery.process_due_deliveries(session, notifier, now=later))
    assert attempted_second == 1
    refreshed = crud.get_notification_delivery(session, event_id=event_id, provider="discord")
    assert refreshed.status == "failed_retryable"  # still one fail_times=2 left

    even_later = later + timedelta(hours=1)
    attempted_third = asyncio.run(
        delivery.process_due_deliveries(session, notifier, now=even_later)
    )
    assert attempted_third == 1
    refreshed = crud.get_notification_delivery(session, event_id=event_id, provider="discord")
    assert refreshed.status == "sent"
    assert notifier.calls == 3
    assert len(notifier.sent_embeds) == 1


def test_process_due_deliveries_backfills_event_with_no_delivery_row(session: Session) -> None:
    """The narrow crash window this module closes: an EventRecord exists
    with no NotificationDelivery row at all yet (as if the process died
    between the two)."""
    event_id, listing_id = _seed_event(session)
    assert crud.get_notification_delivery(session, event_id=event_id, provider="discord") is None

    notifier = RecordingNotifier()
    attempted = asyncio.run(delivery.process_due_deliveries(session, notifier))

    assert attempted == 1
    refreshed = crud.get_notification_delivery(session, event_id=event_id, provider="discord")
    assert refreshed is not None
    assert refreshed.status == "sent"
    assert len(notifier.sent_embeds) == 1
    payload = json.loads(refreshed.payload_json)
    assert any(f.get("value") for f in payload.get("fields", []))
