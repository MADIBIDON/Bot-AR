"""purchase/merchants/cultura.py — Phase 39.

No real network for these unit tests: httpx.post is monkeypatched to
return locally-built httpx.Response objects shaped exactly like the
real Cultura Magento 2 GraphQL responses captured live this session
(createEmptyCart, addSimpleProductsToCart, products-by-url_key) via a
genuine, one-time, manual browser action — never scripted automation of
their site. No real cart is ever created by these tests, no real order
is ever touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from purchase.base import HumanActionRequiredError, PurchaseError, StaleListingError
from purchase.merchants.cultura import CulturaPurchaseConnector
from purchase.models import PurchaseIntent

_URL = "https://www.cultura.com/p-pokemon-ev08-coffret-dresseur-d-elite-10816948.html"


def _intent(**overrides: object) -> PurchaseIntent:
    defaults: dict[str, object] = dict(
        watch_rule_id=1,
        product_id=1,
        listing_id=1,
        merchant="Cultura",
        product_name="EV08 coffret Dresseur d'Elite - Pokémon",
        url=_URL,
        observed_price=Decimal("55.99"),
        max_price_allowed=Decimal("100"),
        quantity=1,
        match_confidence=100,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return PurchaseIntent(**defaults)  # type: ignore[arg-type]


def _product_lookup_response(
    *, sku: str = "10816948", price: float = 55.99, ean: str = "0820650559259"
) -> dict:
    return {
        "data": {
            "products": {
                "items": [
                    {
                        "sku": sku,
                        "name": "EV08 coffret Dresseur d'Elite - Pokémon",
                        "ean": ean,
                        "price_range": {
                            "minimum_price": {"final_price": {"value": price, "currency": "EUR"}}
                        },
                        "stock_item_extra": {"front_availability": "unavailable"},
                    }
                ]
            }
        }
    }


def _create_cart_response() -> dict:
    return {"data": {"createEmptyCart": "vfCjwh4Af4cAETlpm5l6tkPXk4NR2Q4n"}}


def _add_to_cart_response(
    *, quantity_available: int | None = 1, row_total: float = 55.99, cart_error: str | None = None
) -> dict:
    return {
        "data": {
            "addSimpleProductsToCart": {
                "cart": {
                    "id": "vfCjwh4Af4cAETlpm5l6tkPXk4NR2Q4n",
                    "cart_error": cart_error,
                    "items": []
                    if cart_error
                    else [
                        {
                            "quantity": 1,
                            "quantity_available": quantity_available,
                            "product": {
                                "sku": "10816948",
                                "ean": "0820650559259",
                                "name": "EV08 coffret Dresseur d'Elite - Pokémon",
                            },
                            "prices": {
                                "row_total_including_tax": {"value": row_total, "currency": "EUR"}
                            },
                        }
                    ],
                }
            }
        }
    }


def _router(*, lookup: dict, cart: dict | None = None, add: dict | None = None):
    calls: list[str] = []

    def fake_post(url: str, *, json: dict, **kwargs: object) -> httpx.Response:
        query = json["query"]
        calls.append(query)
        if "products(filter" in query:
            body = lookup
        elif "createEmptyCart" in query:
            body = cart or _create_cart_response()
        elif "addSimpleProductsToCart" in query:
            body = add or _add_to_cart_response()
        else:
            raise AssertionError(f"unexpected query: {query[:60]}")
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    return fake_post, calls


def test_revalidate_success_returns_real_cart_price_and_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = CulturaPurchaseConnector()
    fake_post, calls = _router(lookup=_product_lookup_response())
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent())

    assert result.available is True
    assert result.price == Decimal("55.99")
    assert result.quantity_available == 1
    assert len(calls) == 3  # lookup, createEmptyCart, addSimpleProductsToCart


def test_revalidate_uses_the_cart_price_not_the_catalog_search_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real nuance observed live: a marketplace offer's real cart price
    can differ from the catalog search's price_range. revalidate() must
    trust the cart, not the lookup."""
    connector = CulturaPurchaseConnector()
    fake_post, _ = _router(
        lookup=_product_lookup_response(price=55.99),
        add=_add_to_cart_response(row_total=188.60),
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent())

    assert result.price == Decimal("188.60")


def test_revalidate_scales_price_by_quantity(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = CulturaPurchaseConnector()
    fake_post, _ = _router(
        lookup=_product_lookup_response(),
        add=_add_to_cart_response(row_total=111.98),  # 2x 55.99
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent(quantity=2))

    assert result.price == Decimal("55.99")


def test_no_product_found_raises_stale_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = CulturaPurchaseConnector()

    def fake_post(url: str, *, json: dict, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"products": {"items": []}}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(StaleListingError, match="no longer exists"):
        connector.revalidate(_intent())


def test_ambiguous_url_key_raises_stale_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = CulturaPurchaseConnector()
    two_items = _product_lookup_response()
    two_items["data"]["products"]["items"].append(two_items["data"]["products"]["items"][0])

    def fake_post(url: str, *, json: dict, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=two_items, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(StaleListingError, match="ambiguous"):
        connector.revalidate(_intent())


def test_cart_error_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = CulturaPurchaseConnector()
    fake_post, _ = _router(
        lookup=_product_lookup_response(),
        add=_add_to_cart_response(cart_error="This product is out of stock."),
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent())

    assert result.available is False


def test_zero_quantity_available_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = CulturaPurchaseConnector()
    fake_post, _ = _router(
        lookup=_product_lookup_response(), add=_add_to_cart_response(quantity_available=0)
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent())

    assert result.available is False


def test_unknown_quantity_available_defaults_to_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """quantity_available=None means "unknown" — never treated as
    out-of-stock, matching purchase/models.py's own documented
    convention for RevalidationResult.quantity_available."""
    connector = CulturaPurchaseConnector()
    fake_post, _ = _router(
        lookup=_product_lookup_response(), add=_add_to_cart_response(quantity_available=None)
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent())

    assert result.available is True


def test_checkout_always_raises_human_action_required() -> None:
    connector = CulturaPurchaseConnector()

    from purchase.models import RevalidationResult

    with pytest.raises(HumanActionRequiredError):
        connector.checkout(
            _intent(),
            RevalidationResult(
                available=True, price=Decimal("55.99"), shipping_cost=None, quantity_available=1
            ),
        )


def test_network_error_raises_purchase_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = CulturaPurchaseConnector()

    def fake_post(url: str, *, json: dict, **kwargs: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PurchaseError, match="timeout"):
        connector.revalidate(_intent())


def test_graphql_error_raises_purchase_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = CulturaPurchaseConnector()

    def fake_post(url: str, *, json: dict, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"errors": [{"message": "Query complexity exceeded"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PurchaseError, match="GraphQL error"):
        connector.revalidate(_intent())


def test_uses_provided_client_instead_of_httpx_post(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("httpx.post must not be called when a client is provided")

    monkeypatch.setattr(httpx, "post", fail_if_called)
    calls: list[str] = []

    class _FakeClient:
        def post(self, url: str, *, json: dict, **kwargs: object) -> httpx.Response:
            calls.append(json["query"])
            query = json["query"]
            if "products(filter" in query:
                body = _product_lookup_response()
            elif "createEmptyCart" in query:
                body = _create_cart_response()
            else:
                body = _add_to_cart_response()
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    connector = CulturaPurchaseConnector(client=_FakeClient())

    result = connector.revalidate(_intent())

    assert result.available is True
    assert len(calls) == 3


def test_warm_up_never_raises_when_merchant_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, *, json: dict, **kwargs: object):
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    connector = CulturaPurchaseConnector()

    connector.warm_up()  # must not raise


def test_warm_up_makes_a_real_shaped_lookup_query(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_post(url: str, *, json: dict, **kwargs: object) -> httpx.Response:
        calls.append(json["query"])
        return httpx.Response(
            200, json={"data": {"products": {"items": []}}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    connector = CulturaPurchaseConnector()

    connector.warm_up()

    assert len(calls) == 1
    assert "products(filter" in calls[0]


# --- Full integration through the real, unmodified purchase engine -----


def test_full_attempt_purchase_reaches_human_action_required(
    monkeypatch: pytest.MonkeyPatch, session
) -> None:
    """Proves CulturaPurchaseConnector genuinely integrates with the
    existing, unmodified purchase/engine.py::attempt_purchase() — same
    ABC, same kill-switch re-check, same claim, same everything already
    proven for Fuji Store/Shopify UCP. No real network, no real cart."""
    import asyncio

    from database import crud
    from products.matcher import MatchResult
    from products.observation import ProductObservation
    from purchase.config import PurchasePolicy
    from purchase.engine import attempt_purchase
    from purchase.registry import PurchaseConnectorRegistry

    product = crud.create_product(
        session, "EV08 coffret Dresseur d'Elite - Pokémon", ean="0820650559259"
    )
    merchant = crud.create_merchant(session, "Cultura")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url=_URL,
        external_id="p-pokemon-ev08-coffret-dresseur-d-elite-10816948.html",
    )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=30,
        max_quantity=1,
        max_price=Decimal("100"),
    )

    fake_post, _ = _router(lookup=_product_lookup_response())
    monkeypatch.setattr(httpx, "post", fake_post)

    registry = PurchaseConnectorRegistry()
    registry.register("Cultura", CulturaPurchaseConnector())

    class FakeNotifier:
        def __init__(self) -> None:
            self.sent_embeds: list[object] = []

        async def send_embed(self, embed: object) -> None:
            self.sent_embeds.append(embed)

    observation = ProductObservation(
        merchant="Cultura",
        external_id=listing.external_id,
        name=product.name,
        price=Decimal("55.99"),
        currency="EUR",
        available=True,
        url=_URL,
        observed_at=datetime.now(UTC),
        ean="0820650559259",
    )
    match = MatchResult(matched=True, confidence=100, method="ean_exact", reason="test")
    policy = PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({"cultura.com", "www.cultura.com"}),
        cooldown_seconds=0,
    )
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            observation,
            match,
            policy,
            registry,
            ("cultura.com",),
            notifier,
        )
    )

    assert outcome.status.value == "human_action_required"
    assert "HUMAN ACTION REQUIRED" in [e.title for e in notifier.sent_embeds]
