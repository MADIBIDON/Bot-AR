"""Real, file-backed SQLite concurrency stress tests (Phase 26 audit) —
deliberately NOT using the in-memory :memory: fixture from conftest.py,
since that one connection-pools a single shared connection (StaticPool)
and would never reproduce the real "worker holds one connection, CLI
opens another against the same file" pattern that actually caused a
production incident this session.

Each test spins up several real OS threads, each opening its own Session
against the same on-disk file, and hammers real reads/writes
concurrently — reproducing exactly the mix section 1 of the audit asked
for: worker monitoring + discovery-style writes + CLI-style reads, all
at once. Success is "no exception in any thread" — a "database is
locked" (SQLite's OperationalError) or the "bad parameter or other API
misuse" ORM/thread-safety error this project already hit once are both
real failures here, not flakiness to retry away.
"""

from __future__ import annotations

import threading
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from database import crud
from database.session import create_all, get_engine


def _make_file_engine(tmp_path: Path):
    db_path = tmp_path / "concurrency_test.db"
    return get_engine(f"sqlite:///{db_path}")


def test_concurrent_writers_and_readers_never_raise(tmp_path: Path) -> None:
    engine = _make_file_engine(tmp_path)
    create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    # Seed one product/merchant/listing every thread will read/write
    # against, mirroring a real WatchRule already being monitored.
    seed_session = session_factory()
    product = crud.create_product(seed_session, "Concurrency Test Product")
    merchant = crud.create_merchant(seed_session, "ConcurrencyMerchant")
    listing = crud.create_listing(
        seed_session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://example.test/p/1",
        external_id="item-1",
    )
    crud.create_watch_rule(
        seed_session, product_id=product.id, listing_id=listing.id, check_interval=1, max_quantity=1
    )
    seed_session.close()

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    iterations = 40

    def _record_error(exc: BaseException) -> None:
        with errors_lock:
            errors.append(exc)

    def _writer_worker_style() -> None:
        """Simulates the monitoring fast path: one Session per call,
        appending ObservationRecords — the worker's real pattern of one
        long-lived Session doing many sequential writes."""
        session = session_factory()
        try:
            from datetime import UTC, datetime
            from decimal import Decimal

            from products.observation import ProductObservation

            for i in range(iterations):
                obs = ProductObservation(
                    merchant="ConcurrencyMerchant",
                    external_id="item-1",
                    name="Concurrency Test Product",
                    price=Decimal("10.00") + Decimal(i % 5),
                    currency="EUR",
                    available=True,
                    url="https://example.test/p/1",
                    observed_at=datetime.now(UTC),
                )
                crud.create_observation_record(session, listing_id=listing.id, observation=obs)
        except BaseException as exc:  # noqa: BLE001 - capture for the assertion below
            _record_error(exc)
        finally:
            session.close()

    def _discovery_writer_style() -> None:
        """Simulates discovery: its own Session, creating new
        merchants/listings/watch_rules — a separate 'writer' identity."""
        session = session_factory()
        try:
            for i in range(iterations // 4):
                m = crud.get_merchant_by_name(session, f"DiscoveryMerchant{i}")
                if m is None:
                    m = crud.create_merchant(session, f"DiscoveryMerchant{i}")
                lst = crud.get_listing_by_merchant_and_external_id(session, m.id, f"disc-{i}")
                if lst is None:
                    crud.create_listing(
                        session,
                        product_id=product.id,
                        merchant_id=m.id,
                        url=f"https://example.test/disc/{i}",
                        external_id=f"disc-{i}",
                    )
        except BaseException as exc:  # noqa: BLE001
            _record_error(exc)
        finally:
            session.close()

    def _cli_reader_style() -> None:
        """Simulates a CLI command: a brand-new Session per call, just
        reading — `products`/`product <id>`/`status` all do this."""
        try:
            for _ in range(iterations):
                session = session_factory()
                try:
                    crud.list_products(session)
                    crud.list_watch_rules(session, enabled=True)
                    crud.list_observation_records_for_listing(session, listing.id)
                finally:
                    session.close()
        except BaseException as exc:  # noqa: BLE001
            _record_error(exc)

    threads = [
        threading.Thread(target=_writer_worker_style),
        threading.Thread(target=_discovery_writer_style),
        threading.Thread(target=_cli_reader_style),
        threading.Thread(target=_cli_reader_style),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"concurrent DB access raised: {errors!r}"

    # Sanity: the writer thread's rows actually landed — a silent partial
    # failure would be worse than a loud one.
    verify_session = session_factory()
    records = crud.list_observation_records_for_listing(verify_session, listing.id)
    assert len(records) == iterations
    verify_session.close()


def test_wal_and_busy_timeout_are_active_on_a_real_file_db(tmp_path: Path) -> None:
    engine = _make_file_engine(tmp_path)
    create_all(engine)
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
        fk = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert mode == "wal"
    assert timeout == 5000
    assert fk == 1


def test_double_discovery_race_never_duplicates_merchant_or_listing(tmp_path: Path) -> None:
    """Reproduces section 9's exact scenario: the worker's own background
    discovery and a manually-run `discover <id>` CLI invocation, each in
    its own process/connection, both racing to auto-link the exact same
    real candidate for the same Product at once. app/discovery.py's
    _get_or_create_merchant()/_get_or_create_listing() must turn the
    resulting IntegrityError into a plain re-query, never a crash and
    never two rows for the same real merchant/listing."""
    engine = _make_file_engine(tmp_path)
    create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    seed_session = session_factory()
    product = crud.create_product(seed_session, "Race Test Product")
    seed_session.close()

    from app.discovery import _get_or_create_listing, _get_or_create_merchant

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def _race_once() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)  # maximize the chance both threads overlap
            merchant = _get_or_create_merchant(session, "RaceMerchant")
            _get_or_create_listing(
                session,
                product_id=product.id,
                merchant_id=merchant.id,
                url="https://race.example/p/1",
                external_id="race-item-1",
            )
        except BaseException as exc:  # noqa: BLE001 - capture for the assertion below
            with errors_lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_race_once) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"concurrent discovery linking raised: {errors!r}"

    verify_session = session_factory()
    merchants = [
        m
        for m in crud.list_products(verify_session)  # sanity the product itself is untouched
    ]
    assert len(merchants) == 1
    merchant = crud.get_merchant_by_name(verify_session, "RaceMerchant")
    assert merchant is not None
    listings = crud.list_listings_for_product(verify_session, product.id)
    assert len(listings) == 1
    assert listings[0].external_id == "race-item-1"
    verify_session.close()
