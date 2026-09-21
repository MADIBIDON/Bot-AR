"""app/keyword_watch.py — the catalogue watch. No real network: fake
discovery sources are registered directly.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy.orm import Session

from app.keyword_watch import notify_keyword_finds, run_keyword_watch
from connectors.base import ConnectorProduct
from database import crud
from discovery.base import DiscoveryError, DiscoveryUnavailableError, RetailDiscoverySource
from discovery.registry import DiscoveryRegistry


class _FakeSource(RetailDiscoverySource):
    def __init__(self, products: list[ConnectorProduct] | Exception) -> None:
        self._products = products
        self.calls = 0

    def search(self, query, *, ean=None, mpn=None, limit=10):
        self.calls += 1
        if isinstance(self._products, Exception):
            raise self._products
        return list(self._products)


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


def _product(
    *, external_id: str = "etb-30", price: str = "64.99", available: bool = True
) -> ConnectorProduct:
    return ConnectorProduct(
        external_id=external_id,
        name="Coffret dresseur d'élite 30 ans",
        price=Decimal(price),
        currency="EUR",
        available=available,
        seller="Shop",
        url=f"https://shop.example/products/{external_id}",
        image_url="https://shop.example/img.jpg",
    )


def _registry(**sources: _FakeSource) -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    for name, source in sources.items():
        registry.register(name, source)
    return registry


def test_first_run_reports_every_in_stock_hit_as_new(session: Session) -> None:
    watch = crud.create_keyword_watch(session, "pokemon 30 ans")
    registry = _registry(Shop=_FakeSource([_product()]))

    result = run_keyword_watch(session, watch, registry)

    assert len(result.finds) == 1
    assert result.finds[0].reason == "new"
    assert result.searched_merchants == 1


def test_second_run_is_silent(session: Session) -> None:
    """The whole point: a standing search must announce the catalogue
    once, not on every cycle."""
    watch = crud.create_keyword_watch(session, "pokemon 30 ans")
    registry = _registry(Shop=_FakeSource([_product()]))

    run_keyword_watch(session, watch, registry)
    second = run_keyword_watch(session, watch, registry)

    assert second.finds == ()


def test_restock_of_a_known_item_is_reported(session: Session) -> None:
    watch = crud.create_keyword_watch(session, "pokemon 30 ans")
    out_of_stock = _registry(Shop=_FakeSource([_product(available=False)]))
    run_keyword_watch(session, watch, out_of_stock)

    back = _registry(Shop=_FakeSource([_product(available=True)]))
    result = run_keyword_watch(session, watch, back)

    assert [f.reason for f in result.finds] == ["restock"]


def test_new_but_out_of_stock_is_recorded_without_alerting(session: Session) -> None:
    """Not actionable now, but remembering it is what makes the later
    restock detectable."""
    watch = crud.create_keyword_watch(session, "pokemon 30 ans")
    registry = _registry(Shop=_FakeSource([_product(available=False)]))

    result = run_keyword_watch(session, watch, registry)

    assert result.finds == ()
    assert (
        crud.get_keyword_watch_seen(
            session, keyword_watch_id=watch.id, merchant="Shop", external_id="etb-30"
        )
        is not None
    )


def test_hits_above_max_price_are_suppressed(session: Session) -> None:
    watch = crud.create_keyword_watch(session, "pokemon", max_price=Decimal("50"))
    registry = _registry(Shop=_FakeSource([_product(price="64.99")]))

    result = run_keyword_watch(session, watch, registry)

    assert result.finds == ()
    assert result.suppressed_over_max_price == 1


def test_one_failing_merchant_never_stops_the_others(session: Session) -> None:
    watch = crud.create_keyword_watch(session, "pokemon 30 ans")
    registry = _registry(
        Broken=_FakeSource(DiscoveryError("boom")),
        RateLimited=_FakeSource(DiscoveryUnavailableError("429")),
        Working=_FakeSource([_product()]),
    )

    result = run_keyword_watch(session, watch, registry)

    assert len(result.finds) == 1
    assert result.finds[0].merchant == "Working"
    assert {m for m, _ in result.failed_merchants} == {"Broken", "RateLimited"}


def test_run_records_last_searched_at(session: Session) -> None:
    watch = crud.create_keyword_watch(session, "pokemon")
    run_keyword_watch(session, watch, _registry(Shop=_FakeSource([])))

    assert crud.get_keyword_watch(session, watch.id).last_searched_at is not None


def test_never_creates_products_or_watch_rules(session: Session) -> None:
    """A find is an alert, not an auto-linked purchase target — promoting
    one stays a human decision."""
    watch = crud.create_keyword_watch(session, "pokemon 30 ans")
    run_keyword_watch(session, watch, _registry(Shop=_FakeSource([_product()])))

    assert crud.list_products(session) == []
    assert crud.list_watch_rules(session) == []


def test_notify_sends_one_embed_per_find(session: Session) -> None:
    watch = crud.create_keyword_watch(session, "pokemon 30 ans")
    registry = _registry(Shop=_FakeSource([_product(external_id="a"), _product(external_id="b")]))
    result = run_keyword_watch(session, watch, registry)
    notifier = _FakeNotifier()

    sent = asyncio.run(notify_keyword_finds(result, notifier))

    assert sent == 2
    assert len(notifier.sent_embeds) == 2
    assert "Coffret dresseur" in notifier.sent_embeds[0].description


def test_notify_survives_a_broken_discord(session: Session) -> None:
    watch = crud.create_keyword_watch(session, "pokemon 30 ans")
    result = run_keyword_watch(session, watch, _registry(Shop=_FakeSource([_product()])))

    class _Broken:
        async def send_embed(self, embed: object) -> None:
            raise RuntimeError("discord down")

    # Must not raise — a lost alert never takes the watch loop down.
    asyncio.run(notify_keyword_finds(result, _Broken()))
