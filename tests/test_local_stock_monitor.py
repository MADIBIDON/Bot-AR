"""local_stock/monitor.py — no real network: a fake RbsPlatformClient
stands in for local_stock.rbs_platform.RbsPlatformClient (same shape,
same pattern as connectors/fake_store.py standing in for a real
merchant connector elsewhere in this project)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from database import crud
from local_stock.delivery import process_due_local_deliveries
from local_stock.monitor import check_local_stock_for_listing
from local_stock.rbs_platform import RbsStoreStock


class FakeRbsClient:
    def __init__(self, *, pickup_store_ids: set[str] | None = None) -> None:
        self.pickup_store_ids = pickup_store_ids or set()
        self.calls = 0

    def check_pickup_availability(self, *, sku, latitude, longitude, radius_km=3000):
        self.calls += 1
        return [
            RbsStoreStock(external_store_id=sid, can_pick_up=True) for sid in self.pickup_store_ids
        ]


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


def _setup(session: Session):
    product = crud.create_product(session, "Duopack Evoli 30 ans", ean="1234567890123")
    merchant = crud.create_merchant(session, "JouéClub")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://www.joueclub.fr/p/fake",
        external_id="fake",
    )
    now = datetime.now(UTC)
    paris = crud.upsert_retail_store(
        session,
        retailer="JouéClub",
        external_store_id="1001",
        name="JouéClub PARIS",
        city="PARIS",
        postal_code="75002",
        latitude=Decimal("48.87"),
        longitude=Decimal("2.34"),
        now=now,
    )
    lorient = crud.upsert_retail_store(
        session,
        retailer="JouéClub",
        external_store_id="1002",
        name="JouéClub LORIENT",
        city="LORIENT",
        postal_code="56100",
        latitude=Decimal("47.75"),
        longitude=Decimal("-3.36"),
        now=now,
    )
    return listing, paris, lorient


def test_first_check_is_a_baseline_never_an_alert(session: Session) -> None:
    listing, paris, lorient = _setup(session)
    client = FakeRbsClient(pickup_store_ids={"1001"})

    checked = check_local_stock_for_listing(
        session,
        client=client,
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price="59.99 EUR",
    )

    assert checked == 2
    assert crud.list_due_local_deliveries(session, now=datetime.now(UTC)) == []
    paris_state = crud.get_local_stock_state(session, store_id=paris.id, listing_id=listing.id)
    assert paris_state.stock_state == "click_and_collect"
    lorient_state = crud.get_local_stock_state(session, store_id=lorient.id, listing_id=listing.id)
    assert lorient_state.stock_state == "out_of_stock"


def test_out_of_stock_to_click_and_collect_fires_exactly_one_alert(session: Session) -> None:
    listing, paris, lorient = _setup(session)

    check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids=set()),  # baseline: nobody has it
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price="59.99 EUR",
    )
    check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids={"1001"}),  # Paris now has it
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price="59.99 EUR",
    )

    due = crud.list_due_local_deliveries(session, now=datetime.now(UTC))
    assert len(due) == 1


def test_click_and_collect_to_click_and_collect_never_spams(session: Session) -> None:
    listing, paris, lorient = _setup(session)

    check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids=set()),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price="59.99 EUR",
    )
    for _ in range(3):
        check_local_stock_for_listing(
            session,
            client=FakeRbsClient(pickup_store_ids={"1001"}),
            retailer="JouéClub",
            listing=listing,
            sku="1234567890123",
            product_name="Duopack Evoli 30 ans",
            price="59.99 EUR",
        )

    due = crud.list_due_local_deliveries(session, now=datetime.now(UTC))
    assert len(due) == 1  # only the OUT_OF_STOCK -> CLICK_AND_COLLECT transition, once


def test_click_and_collect_to_out_of_stock_records_state_no_alert(session: Session) -> None:
    listing, paris, lorient = _setup(session)

    check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids=set()),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price=None,
    )
    check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids={"1001"}),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price=None,
    )
    check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids=set()),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price=None,
    )

    paris_state = crud.get_local_stock_state(session, store_id=paris.id, listing_id=listing.id)
    assert paris_state.stock_state == "out_of_stock"
    due = crud.list_due_local_deliveries(session, now=datetime.now(UTC))
    assert len(due) == 1  # still just the original OUT->C&C alert; going back out never adds one


def test_a_disabled_store_is_excluded_from_checks(session: Session) -> None:
    from database.models import RetailStore

    listing, paris, lorient = _setup(session)
    lorient_row = session.get(RetailStore, lorient.id)
    lorient_row.enabled = False
    session.commit()

    checked = check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids={"1001"}),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price=None,
    )

    assert checked == 1  # Lorient excluded


def test_online_and_local_events_stay_in_separate_tables(session: Session) -> None:
    """A local-stock delivery must never collide with, or be confused
    for, an online NotificationDelivery — separate ID spaces, separate
    tables (see database/models.py::LocalNotificationDelivery)."""
    listing, paris, lorient = _setup(session)
    check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids=set()),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price=None,
    )
    check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids={"1001"}),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price=None,
    )

    assert crud.notification_delivery_status_counts(session) == {}  # online table untouched
    local_due = crud.list_due_local_deliveries(session, now=datetime.now(UTC))
    assert len(local_due) == 1


def test_local_delivery_actually_sends_and_embed_has_the_right_fields(session: Session) -> None:
    listing, paris, lorient = _setup(session)
    check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids=set()),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price="59.99 EUR",
    )
    check_local_stock_for_listing(
        session,
        client=FakeRbsClient(pickup_store_ids={"1001"}),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price="59.99 EUR",
    )

    notifier = RecordingNotifier()
    attempted = asyncio.run(process_due_local_deliveries(session, notifier))

    assert attempted == 1
    assert len(notifier.sent_embeds) == 1
    payload = notifier.sent_embeds[0].to_dict()
    field_names = {f["name"] for f in payload["fields"]}
    assert {"Product", "Retailer", "Store", "City", "Status", "Click & Collect"} <= field_names


def test_restart_durable_local_alert_survives_and_sends_once(tmp_path) -> None:
    """Same restart-simulation pattern as tests/test_delivery_integration.py:
    fresh Session against the same on-disk SQLite file."""
    from sqlalchemy.orm import sessionmaker

    from database.session import create_all, get_engine

    db_path = tmp_path / "local_restart_test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    session_before_crash = session_factory()
    listing, paris, lorient = _setup(session_before_crash)
    check_local_stock_for_listing(
        session_before_crash,
        client=FakeRbsClient(pickup_store_ids=set()),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price=None,
    )
    check_local_stock_for_listing(
        session_before_crash,
        client=FakeRbsClient(pickup_store_ids={"1001"}),
        retailer="JouéClub",
        listing=listing,
        sku="1234567890123",
        product_name="Duopack Evoli 30 ans",
        price=None,
    )
    session_before_crash.close()

    session_after_restart = session_factory()
    notifier = RecordingNotifier()
    attempted = asyncio.run(process_due_local_deliveries(session_after_restart, notifier))

    assert attempted == 1
    assert len(notifier.sent_embeds) == 1

    again = asyncio.run(process_due_local_deliveries(session_after_restart, notifier))
    assert again == 0
    assert len(notifier.sent_embeds) == 1  # not sent twice
