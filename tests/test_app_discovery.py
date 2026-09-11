"""No real network: fake RetailDiscoverySource implementations stand in
for real merchants, exercising app/discovery.py's orchestration
(matching, linking, idempotency) against the real in-memory test DB.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.discovery import DiscoveryRunResult, is_discovery_due, run_discovery_for_product
from connectors.base import ConnectorProduct
from database import crud
from discovery.base import DiscoveryError, DiscoveryUnavailableError, RetailDiscoverySource
from discovery.registry import DiscoveryRegistry


def _run_discovery(*args: object, **kwargs: object) -> DiscoveryRunResult:
    """run_discovery_for_product is async (Phase 22 perf fix: its network
    search is offloaded to a thread so it never blocks the fast path) —
    tests call it the same way app/worker.py's background task does."""
    return asyncio.run(run_discovery_for_product(*args, **kwargs))  # type: ignore[arg-type]


class FakeDiscoverySource(RetailDiscoverySource):
    def __init__(
        self, *, results: list[ConnectorProduct] | None = None, error: Exception | None = None
    ) -> None:
        self._results = results or []
        self._error = error
        self.calls = 0

    def search(self, query, *, ean=None, mpn=None, limit=10):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._results


def _candidate(**overrides: object) -> ConnectorProduct:
    defaults: dict[str, object] = dict(
        external_id="etb-chaos-ascendant-fr",
        name="ETB Pokemon Chaos Ascendant FR",
        price=Decimal("59.90"),
        currency="EUR",
        available=True,
        seller="Kairyu",
        url="https://kairyu.fr/products/etb-chaos-ascendant-fr",
        ean=None,
        mpn=None,
    )
    defaults.update(overrides)
    return ConnectorProduct(**defaults)  # type: ignore[arg-type]


def _registry(**sources: RetailDiscoverySource) -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    for name, source in sources.items():
        registry.register(name, source)
    return registry


def test_auto_link_creates_merchant_listing_and_watch_rule(session: Session) -> None:
    product = crud.create_product(
        session, "ETB Pokemon Chaos Ascendant FR", max_price=Decimal("60")
    )
    source = FakeDiscoverySource(results=[_candidate()])
    registry = _registry(Kairyu=source)

    result = _run_discovery(session, product, registry)

    assert len(result.merchants) == 1
    outcome = result.merchants[0]
    assert outcome.status == "ok"
    assert len(outcome.candidates) == 1
    candidate_result = outcome.candidates[0]
    assert candidate_result.verdict == "auto_link"
    assert candidate_result.watch_rule_id is not None

    rules = crud.list_watch_rules(session, product_id=product.id)
    assert len(rules) == 1
    assert rules[0].listing.merchant.name == "Kairyu"
    assert rules[0].max_price is None  # shares the product's ceiling, not its own


def test_candidate_verdict_creates_no_listing_or_rule(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    source = FakeDiscoverySource(
        results=[_candidate(name="Elite Trainer Box Chaos Ascendant Pokemon France")]
    )
    registry = _registry(Kairyu=source)

    result = _run_discovery(session, product, registry)

    assert result.merchants[0].candidates[0].verdict == "candidate"
    assert result.merchants[0].candidates[0].listing_id is None
    assert crud.list_watch_rules(session, product_id=product.id) == []


def test_no_match_verdict_creates_no_listing_or_rule(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    source = FakeDiscoverySource(results=[_candidate(name="Display Pokemon Chaos Ascendant FR")])
    registry = _registry(Kairyu=source)

    result = _run_discovery(session, product, registry)

    assert result.merchants[0].candidates[0].verdict == "no_match"
    assert crud.list_watch_rules(session, product_id=product.id) == []


def test_discovery_unavailable_for_one_merchant_does_not_stop_others(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    unavailable = FakeDiscoverySource(error=DiscoveryUnavailableError("no profile url"))
    working = FakeDiscoverySource(results=[_candidate(seller="RelicTCG")])
    registry = _registry(Kairyu=unavailable, RelicTCG=working)

    result = _run_discovery(session, product, registry)

    statuses = {m.merchant: m.status for m in result.merchants}
    assert statuses == {"Kairyu": "unavailable", "RelicTCG": "ok"}
    assert working.calls == 1


def test_discovery_error_for_one_merchant_does_not_stop_others(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    failing = FakeDiscoverySource(error=DiscoveryError("network broke"))
    working = FakeDiscoverySource(results=[_candidate()])
    registry = _registry(Kairyu=failing, RelicTCG=working)

    result = _run_discovery(session, product, registry)

    statuses = {m.merchant: m.status for m in result.merchants}
    assert statuses == {"Kairyu": "error", "RelicTCG": "ok"}


def test_no_listing_today_leaves_rule_active_for_next_run(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    empty = FakeDiscoverySource(results=[])
    registry = _registry(Kairyu=empty)

    result = _run_discovery(session, product, registry)

    assert result.merchants[0].status == "ok"
    assert result.merchants[0].candidates == []
    assert crud.list_watch_rules(session, product_id=product.id) == []


def test_listing_discovered_later_gets_linked_on_next_run(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    empty_then_found = FakeDiscoverySource(results=[])
    registry = _registry(Kairyu=empty_then_found)

    _run_discovery(session, product, registry)
    assert crud.list_watch_rules(session, product_id=product.id) == []

    empty_then_found._results = [_candidate()]  # merchant now has it
    _run_discovery(session, product, registry)

    assert len(crud.list_watch_rules(session, product_id=product.id)) == 1


def test_rerunning_discovery_does_not_duplicate_listing_or_rule(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    source = FakeDiscoverySource(results=[_candidate()])
    registry = _registry(Kairyu=source)

    _run_discovery(session, product, registry)
    result2 = _run_discovery(session, product, registry)

    assert len(crud.list_watch_rules(session, product_id=product.id)) == 1
    assert len(crud.list_listings_for_product(session, product.id)) == 1
    assert result2.merchants[0].candidates[0].already_monitored is True


def test_one_user_watch_creates_multiple_listings(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    registry = _registry(
        Kairyu=FakeDiscoverySource(results=[_candidate(seller="Kairyu")]),
        RelicTCG=FakeDiscoverySource(
            results=[
                _candidate(
                    seller="RelicTCG",
                    external_id="etb-2",
                    url="https://www.relictcg.com/products/etb-2",
                )
            ]
        ),
    )

    _run_discovery(session, product, registry)

    rules = crud.list_watch_rules(session, product_id=product.id)
    assert len(rules) == 2
    assert {r.listing.merchant.name for r in rules} == {"Kairyu", "RelicTCG"}


def test_last_discovery_at_updated(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    assert product.last_discovery_at is None
    registry = _registry(Kairyu=FakeDiscoverySource(results=[]))
    now = datetime(2026, 1, 1, tzinfo=UTC)

    _run_discovery(session, product, registry, now=now)

    assert product.last_discovery_at == now


def test_is_discovery_due_when_never_run() -> None:
    from database.models import Product

    fresh = Product(id=1, name="x", discovery_interval=1800, last_discovery_at=None)
    assert is_discovery_due(fresh, datetime.now(UTC)) is True


def test_is_discovery_due_respects_interval() -> None:
    from database.models import Product

    now = datetime(2026, 1, 1, tzinfo=UTC)
    recently_run = Product(
        id=1, name="x", discovery_interval=1800, last_discovery_at=now - timedelta(seconds=10)
    )
    assert is_discovery_due(recently_run, now) is False

    overdue = Product(
        id=1, name="x", discovery_interval=1800, last_discovery_at=now - timedelta(seconds=1801)
    )
    assert is_discovery_due(overdue, now) is True
