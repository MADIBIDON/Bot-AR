"""purchase/merchants/cultura.py — Phase 39/40.

No real network for these unit tests: httpx.post is monkeypatched to
return locally-built httpx.Response objects shaped exactly like the
real Cultura Magento 2 GraphQL responses captured live this session
(createEmptyCart, addSimpleProductsToCart, products-by-url_key) via a
genuine, one-time, manual browser action — never scripted automation of
their site — plus the standard, publicly documented Magento 2 core
shipping/billing mutations (Phase 40). No real cart is ever created by
these tests, no real order is ever touched, and no test here ever
configures a payment method or calls placeOrder. Shipping-profile tests
use entirely fake/synthetic values via monkeypatch.setenv — never the
real local profile in .env — matching this project's standing rule that
real personal data never appears in a test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from purchase.base import HumanActionRequiredError, PurchaseError, StaleListingError
from purchase.merchants.cultura import CulturaCheckoutState, CulturaPurchaseConnector
from purchase.models import PurchaseIntent

_FAKE_SHIPPING_ENV_VARS = {
    "PURCHASE_SHIPPING_FIRST_NAME": "Test",
    "PURCHASE_SHIPPING_LAST_NAME": "User",
    "PURCHASE_SHIPPING_ADDRESS": "1 rue de Test",
    "PURCHASE_SHIPPING_CITY": "Testville",
    "PURCHASE_SHIPPING_POSTAL_CODE": "00000",
    "PURCHASE_SHIPPING_COUNTRY": "FR",
}


def _set_fake_shipping_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _FAKE_SHIPPING_ENV_VARS.items():
        monkeypatch.setenv(name, value)


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
    *,
    quantity_available: int | None = 1,
    row_total: float = 55.99,
    cart_error: str | None = None,
    quantity: int = 1,
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
                            "quantity": quantity,
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


def _shipping_address_response(
    *, carrier_code: str = "colissimo", method_code: str = "delivery", amount: float = 4.99
) -> dict:
    return {
        "data": {
            "setShippingAddressesOnCart": {
                "cart": {
                    "shipping_addresses": [
                        {
                            "available_shipping_methods": [
                                {
                                    "carrier_code": carrier_code,
                                    "method_code": method_code,
                                    "amount": {"value": amount, "currency": "EUR"},
                                    "available": True,
                                }
                            ]
                        }
                    ]
                }
            }
        }
    }


def _shipping_method_response(
    *, carrier_code: str = "colissimo", method_code: str = "delivery", amount: float = 4.99
) -> dict:
    return {
        "data": {
            "setShippingMethodsOnCart": {
                "cart": {
                    "shipping_addresses": [
                        {
                            "selected_shipping_method": {
                                "carrier_code": carrier_code,
                                "method_code": method_code,
                                "amount": {"value": amount, "currency": "EUR"},
                            }
                        }
                    ]
                }
            }
        }
    }


def _billing_address_response() -> dict:
    return {
        "data": {
            "setBillingAddressOnCart": {
                "cart": {"billing_address": {"firstname": "Test", "lastname": "User"}}
            }
        }
    }


def _router(
    *,
    lookup: dict,
    cart: dict | None = None,
    add: dict | None = None,
    shipping_address: dict | None = None,
    shipping_method: dict | None = None,
    billing_address: dict | None = None,
):
    calls: list[str] = []

    def fake_post(url: str, *, json: dict, **kwargs: object) -> httpx.Response:
        query = json["query"]
        calls.append(query)
        if "products(filter" in query:
            body = lookup
        elif "createEmptyCart" in query:
            body = cart or _create_cart_response()
        elif "setShippingAddressesOnCart" in query:
            if shipping_address is None:
                raise AssertionError("unexpected setShippingAddressesOnCart call")
            body = shipping_address
        elif "setShippingMethodsOnCart" in query:
            if shipping_method is None:
                raise AssertionError("unexpected setShippingMethodsOnCart call")
            body = shipping_method
        elif "setBillingAddressOnCart" in query:
            if billing_address is None:
                raise AssertionError("unexpected setBillingAddressOnCart call")
            body = billing_address
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
        add=_add_to_cart_response(row_total=111.98, quantity=2),  # 2x 55.99
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent(quantity=2))

    assert result.price == Decimal("55.99")


def test_cart_quantity_mismatch_raises_stale_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 40 section 27: the merchant can silently cap/adjust the
    quantity actually placed in the cart — must always abort, never
    silently proceed with whatever quantity the merchant chose."""
    connector = CulturaPurchaseConnector()
    fake_post, _ = _router(
        lookup=_product_lookup_response(),
        add=_add_to_cart_response(quantity=1),  # merchant capped it at 1
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(StaleListingError, match="does not match"):
        connector.revalidate(_intent(quantity=2))


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


# --- Phase 40: shipping / billing state machine -------------------------


def test_revalidate_resolves_real_shipping_cost_when_profile_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_fake_shipping_profile(monkeypatch)
    connector = CulturaPurchaseConnector()
    fake_post, calls = _router(
        lookup=_product_lookup_response(),
        shipping_address=_shipping_address_response(amount=4.99),
        shipping_method=_shipping_method_response(amount=4.99),
        billing_address=_billing_address_response(),
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent())

    assert result.available is True
    assert result.shipping_cost == Decimal("4.99")
    assert connector.last_checkout_state == CulturaCheckoutState.BILLING_ADDRESS_SET
    assert len(calls) == 6  # lookup, cart, add, shipping addr, shipping method, billing


def test_revalidate_leaves_shipping_none_when_profile_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _FAKE_SHIPPING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    connector = CulturaPurchaseConnector()
    # No shipping/billing fixtures given — _router raises AssertionError
    # if the connector ever calls one of those mutations, so this test
    # doubles as proof no shipping call happens without a local profile.
    fake_post, _ = _router(lookup=_product_lookup_response())
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent())

    assert result.shipping_cost is None
    assert connector.last_checkout_state == CulturaCheckoutState.CART_WITH_PRODUCT


def test_shipping_failure_degrades_gracefully_without_breaking_revalidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shipping-step hiccup (GraphQL error) must never turn a
    successful cart-level revalidation into a false rejection."""
    _set_fake_shipping_profile(monkeypatch)
    connector = CulturaPurchaseConnector()

    def fake_post(url: str, *, json: dict, **kwargs: object) -> httpx.Response:
        query = json["query"]
        if "products(filter" in query:
            body = _product_lookup_response()
        elif "createEmptyCart" in query:
            body = _create_cart_response()
        elif "addSimpleProductsToCart" in query:
            body = _add_to_cart_response()
        elif "setShippingAddressesOnCart" in query:
            return httpx.Response(
                200,
                json={"errors": [{"message": "Address unusable"}]},
                request=httpx.Request("POST", url),
            )
        else:
            raise AssertionError(f"unexpected query: {query[:60]}")
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent())

    assert result.available is True
    assert result.price == Decimal("55.99")
    assert result.shipping_cost is None
    assert connector.last_checkout_state == CulturaCheckoutState.CART_WITH_PRODUCT


def test_picks_cheapest_available_shipping_method(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_fake_shipping_profile(monkeypatch)
    connector = CulturaPurchaseConnector()
    shipping_response = {
        "data": {
            "setShippingAddressesOnCart": {
                "cart": {
                    "shipping_addresses": [
                        {
                            "available_shipping_methods": [
                                {
                                    "carrier_code": "cheap_but_unavailable",
                                    "method_code": "x",
                                    "amount": {"value": 0.01, "currency": "EUR"},
                                    "available": False,
                                },
                                {
                                    "carrier_code": "express",
                                    "method_code": "y",
                                    "amount": {"value": 9.99, "currency": "EUR"},
                                    "available": True,
                                },
                                {
                                    "carrier_code": "standard",
                                    "method_code": "z",
                                    "amount": {"value": 4.99, "currency": "EUR"},
                                    "available": True,
                                },
                            ]
                        }
                    ]
                }
            }
        }
    }
    fake_post, calls = _router(
        lookup=_product_lookup_response(),
        shipping_address=shipping_response,
        shipping_method=_shipping_method_response(carrier_code="standard", amount=4.99),
        billing_address=_billing_address_response(),
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent())

    assert result.shipping_cost == Decimal("4.99")
    method_call = next(c for c in calls if "setShippingMethodsOnCart" in c)
    assert method_call  # the mutation was actually invoked


def test_checkout_never_calls_a_payment_or_order_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive proof, not just a docstring claim: even after shipping
    and billing succeed, checkout() must still raise
    HumanActionRequiredError, and no call this connector ever makes may
    mention a payment/order mutation."""
    _set_fake_shipping_profile(monkeypatch)
    connector = CulturaPurchaseConnector()
    fake_post, calls = _router(
        lookup=_product_lookup_response(),
        shipping_address=_shipping_address_response(),
        shipping_method=_shipping_method_response(),
        billing_address=_billing_address_response(),
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    result = connector.revalidate(_intent())
    with pytest.raises(HumanActionRequiredError):
        connector.checkout(_intent(), result)

    assert connector.last_checkout_state == CulturaCheckoutState.PAYMENT_HUMAN_REQUIRED
    forbidden = ("setPaymentMethodOnCart", "placeOrder", "VaultCardPaymentToken", "adyenPayment")
    for call in calls:
        for term in forbidden:
            assert term not in call


def test_check_payment_readiness_reaches_billing_when_shipping_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_fake_shipping_profile(monkeypatch)
    connector = CulturaPurchaseConnector()
    fake_post, _ = _router(
        lookup=_product_lookup_response(),
        shipping_address=_shipping_address_response(),
        shipping_method=_shipping_method_response(),
        billing_address=_billing_address_response(),
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    readiness = connector.check_payment_readiness(_URL)

    assert readiness.checkout_state == CulturaCheckoutState.BILLING_ADDRESS_SET
    assert readiness.shipping_cost == Decimal("4.99")
    assert readiness.reason is None


def test_check_payment_readiness_reports_reason_when_product_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = CulturaPurchaseConnector()

    def fake_post(url: str, *, json: dict, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"products": {"items": []}}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    readiness = connector.check_payment_readiness(_URL)

    assert readiness.checkout_state == CulturaCheckoutState.EMPTY_CART
    assert readiness.reason is not None
    assert "no longer exists" in readiness.reason


def test_check_payment_readiness_reports_cart_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = CulturaPurchaseConnector()
    fake_post, _ = _router(
        lookup=_product_lookup_response(),
        add=_add_to_cart_response(cart_error="out of stock"),
    )
    monkeypatch.setattr(httpx, "post", fake_post)

    readiness = connector.check_payment_readiness(_URL)

    assert readiness.checkout_state == CulturaCheckoutState.CART_UNAVAILABLE
    assert readiness.reason is not None


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
    assert "🟠 HUMAN ACTION REQUIRED" in [e.title for e in notifier.sent_embeds]
