"""Phase 35 section 13: the hot path must never construct its own HTTP
client — it must always reuse whatever persistent client the connector
(purchase/defaults.py, Phase 34) was already built with. Two checks:
a static guard against a future regression (the hot path module simply
never mentions client construction), and a runtime proof that repeated
attempts through the SAME registry hit the exact same connector/client
object, never a fresh one per call.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from database import crud
from products.matcher import MatchResult
from products.observation import ProductObservation
from purchase.base import CheckoutResult, PurchaseConnector
from purchase.config import PurchasePolicy
from purchase.engine import attempt_purchase
from purchase.models import RevalidationResult
from purchase.registry import PurchaseConnectorRegistry


def test_fast_path_source_never_constructs_an_http_client() -> None:
    source = Path("purchase/fast_path.py").read_text(encoding="utf-8")
    assert "httpx.Client(" not in source
    assert "httpx.AsyncClient(" not in source
    assert "import httpx" not in source  # the hot path has no business touching HTTP at all


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


class _CountingConnector(PurchaseConnector):
    """Stands in for a real connector holding one persistent client —
    revalidate_calls/checkout_calls count real invocations; identity()
    lets the test assert the SAME object served every attempt."""

    def __init__(self) -> None:
        self.revalidate_calls = 0
        self.checkout_calls = 0

    def revalidate(self, intent):
        self.revalidate_calls += 1
        return RevalidationResult(
            available=True,
            price=intent.observed_price,
            shipping_cost=Decimal("0"),
            quantity_available=None,
        )

    def checkout(self, intent, revalidated):
        self.checkout_calls += 1
        return CheckoutResult(
            success=True,
            order_reference=f"ORDER-{self.checkout_calls}",
            final_price=revalidated.price,
            shipping_cost=Decimal("0"),
            total_cost=revalidated.price,
            failure_reason=None,
        )


def _seed_n_listings_of_distinct_products(session: Session, n: int) -> list:
    rules = []
    merchant = crud.create_merchant(session, "Kairyu")
    for i in range(n):
        product = crud.create_product(session, f"Product {i}", ean=f"{i:013d}")
        listing = crud.create_listing(
            session,
            product_id=product.id,
            merchant_id=merchant.id,
            url=f"https://kairyu.fr/products/p{i}",
            external_id=f"p{i}",
        )
        rule = crud.create_watch_rule(
            session,
            product_id=product.id,
            listing_id=listing.id,
            check_interval=60,
            max_quantity=1,
            max_price=Decimal("100"),
        )
        rules.append(rule)
    return rules


def test_repeated_attempts_reuse_the_exact_same_connector_object(session: Session) -> None:
    """Same merchant, N distinct products (no product-level blocking
    between them) — every attempt must go through registry.get("Kairyu"),
    which always returns the one persistent connector instance built at
    startup, never a fresh one per call."""
    n = 5
    rules = _seed_n_listings_of_distinct_products(session, n)
    connector = _CountingConnector()
    registry = PurchaseConnectorRegistry()
    registry.register("Kairyu", connector)
    notifier = FakeNotifier()
    policy = PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({"kairyu.fr"}),
        cooldown_seconds=0,
    )
    match = MatchResult(matched=True, confidence=100, method="ean_exact", reason="test")
    seen_connector_ids: set[int] = set()

    async def scenario():
        for rule in rules:
            fetched = registry.get("Kairyu")
            seen_connector_ids.add(id(fetched))
            observation = ProductObservation(
                merchant="Kairyu",
                external_id=rule.listing.external_id,
                name=rule.product.name,
                price=Decimal("59.90"),
                currency="EUR",
                available=True,
                url=rule.listing.url,
                observed_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            )
            await attempt_purchase(
                session,
                rule,
                observation,
                match,
                policy,
                registry,
                ("kairyu.fr",),
                notifier,
            )

    asyncio.run(scenario())

    assert seen_connector_ids == {id(connector)}  # always the same object, never rebuilt
    assert connector.revalidate_calls == n
    assert connector.checkout_calls == n
