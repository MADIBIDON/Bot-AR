"""No real network: httpx.request is monkeypatched to return locally-built
httpx.Response objects standing in for the real Fuji Store WooCommerce
Store API responses captured during Phase 20 recon. No real cart is ever
touched, no real order is ever created.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from purchase.base import (
    AutomatedCheckoutUnsupportedError,
    HumanActionRequiredError,
    PurchaseError,
    StaleListingError,
)
from purchase.merchants.fuji_store import FujiStorePurchaseConnector
from purchase.models import PurchaseIntent

_CART_HEADERS = {"Nonce": "abc123", "Cart-Token": "fake-cart-token"}


def _intent(**overrides: object) -> PurchaseIntent:
    defaults: dict[str, object] = dict(
        watch_rule_id=1,
        product_id=1,
        listing_id=1,
        merchant="Fuji Store",
        product_name="ALAKAZAM HOLO 1/102",
        url="https://fuji-store.fr/produit/alakazam-holo-1-102-set-de-base-pokemon-fr-1999/",
        observed_price=Decimal("89.90"),
        max_price_allowed=Decimal("100"),
        quantity=1,
        match_confidence=100,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return PurchaseIntent(**defaults)  # type: ignore[arg-type]


def _product_response(
    url: str,
    *,
    has_options: bool = True,
    variations: list[dict] | None = None,
    is_purchasable: bool = True,
    product_id: int = 96027,
) -> httpx.Response:
    body = [
        {
            "id": product_id,
            "slug": "alakazam-holo-1-102-set-de-base-pokemon-fr-1999",
            "has_options": has_options,
            "is_purchasable": is_purchasable,
            "variations": variations if variations is not None else [{"id": 96028}],
        }
    ]
    return httpx.Response(200, json=body, request=httpx.Request("GET", url))


def _cart_response(
    url: str,
    *,
    method: str = "GET",
    item_price_minor: int = 8990,
    quantity: int = 1,
    shipping_minor: int | None = None,
    tax_minor: int | None = None,
    errors: list[dict] | None = None,
    line_item_id: int = 96028,
    quantity_max: int | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    total_items = item_price_minor * quantity
    body = {
        "items": [
            {
                "id": line_item_id,
                "quantity_limits": {"maximum": quantity_max} if quantity_max is not None else {},
            }
        ]
        if not errors
        else [],
        "errors": errors or [],
        "totals": {
            "currency_minor_unit": 2,
            "total_items": str(total_items),
            "total_shipping": str(shipping_minor) if shipping_minor is not None else None,
            "total_tax": str(tax_minor) if tax_minor is not None else "0",
        },
    }
    resp_headers = {**_CART_HEADERS, **(headers or {})}
    return httpx.Response(200, json=body, headers=resp_headers, request=httpx.Request(method, url))


def _empty_cart_get_response(url: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"items": [], "errors": [], "totals": {"currency_minor_unit": 2, "total_items": "0"}},
        headers=_CART_HEADERS,
        request=httpx.Request("GET", url),
    )


def test_add_to_cart_and_total_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url)
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            return _cart_response(url, method="POST")
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    result = connector.revalidate(_intent())

    assert result.available is True
    assert result.price == Decimal("89.90")


def test_single_variant_is_resolved_automatically(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_ids: list[int] = []

    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url, variations=[{"id": 555}])
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            captured_ids.append(kwargs["json"]["id"])
            return _cart_response(url, method="POST", line_item_id=555)
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    connector.revalidate(_intent())

    assert captured_ids == [555]


def test_multiple_variants_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return _product_response(url, variations=[{"id": 1}, {"id": 2}])

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    with pytest.raises(AutomatedCheckoutUnsupportedError, match="variant"):
        connector.revalidate(_intent())


def test_simple_product_without_options_uses_product_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_ids: list[int] = []

    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url, has_options=False, product_id=777)
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            captured_ids.append(kwargs["json"]["id"])
            return _cart_response(url, method="POST", line_item_id=777)
        raise AssertionError

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    connector.revalidate(_intent())

    assert captured_ids == [777]


def test_price_change_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url)
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            return _cart_response(url, method="POST", item_price_minor=9990)  # price rose
        raise AssertionError

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    result = connector.revalidate(_intent(observed_price=Decimal("89.90")))

    assert result.price == Decimal("99.90")


def test_out_of_stock_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url)
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            return _cart_response(
                url, method="POST", errors=[{"message": "Not enough stock available"}]
            )
        raise AssertionError

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    result = connector.revalidate(_intent())

    assert result.available is False


def test_product_no_longer_exists_raises_stale_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    with pytest.raises(StaleListingError):
        connector.revalidate(_intent())


def test_not_purchasable_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return _product_response(url, is_purchasable=False)

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    result = connector.revalidate(_intent())

    assert result.available is False
    assert result.quantity_available == 0


def test_shipping_and_tax_included_when_address_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PURCHASE_SHIPPING_POSTAL_CODE", "75001")
    monkeypatch.setenv("PURCHASE_SHIPPING_COUNTRY", "FR")

    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url)
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            return _cart_response(url, method="POST")
        if url.endswith("/cart/update-customer"):
            return _cart_response(url, method="POST", shipping_minor=255, tax_minor=51)
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    result = connector.revalidate(_intent())

    assert result.shipping_cost == Decimal("2.55")
    assert result.tax_amount == Decimal("0.51")


def test_shipping_unknown_without_configured_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PURCHASE_SHIPPING_POSTAL_CODE", raising=False)

    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url)
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            return _cart_response(url, method="POST")
        raise AssertionError

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    result = connector.revalidate(_intent())

    assert result.shipping_cost is None


def test_shipping_pushing_total_over_max_is_visible_to_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connector just reports the real shipping cost — the actual
    refusal happens in purchase/engine.py's post-revalidation gate,
    already covered in tests/test_purchase_engine_integration.py."""
    monkeypatch.setenv("PURCHASE_SHIPPING_POSTAL_CODE", "75001")

    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url)
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            return _cart_response(url, method="POST", item_price_minor=9500)
        if url.endswith("/cart/update-customer"):
            return _cart_response(url, method="POST", item_price_minor=9500, shipping_minor=964)
        raise AssertionError

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    result = connector.revalidate(_intent(max_price_allowed=Decimal("100")))

    total = result.price * 1 + (result.shipping_cost or Decimal("0"))
    assert total == Decimal("104.64")  # 95.00 + 9.64 > 100 max_price


def test_quantity_available_reported_from_line_item_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url)
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            return _cart_response(url, method="POST", quantity_max=1)
        raise AssertionError

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    result = connector.revalidate(_intent(quantity=1))

    assert result.quantity_available == 1


def test_expired_session_403_yields_human_action_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url)
        return httpx.Response(403, text="Forbidden", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    with pytest.raises(HumanActionRequiredError):
        connector.revalidate(_intent())


def test_captcha_challenge_page_yields_human_action_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url)
        return httpx.Response(
            200,
            text="<html>Checking your browser before accessing... cf-challenge</html>",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    with pytest.raises(HumanActionRequiredError):
        connector.revalidate(_intent())


def test_network_timeout_raises_purchase_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    with pytest.raises(PurchaseError, match="timeout"):
        connector.revalidate(_intent())


def test_checkout_always_requires_human_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """checkout() must never attempt payment — PayPal is the only
    gateway, and completing it always needs a human."""
    connector = FujiStorePurchaseConnector()

    with pytest.raises(HumanActionRequiredError, match="PayPal"):
        connector.checkout(_intent(), revalidated=None)  # type: ignore[arg-type]


def test_no_secret_or_payment_data_in_base_headers() -> None:
    """The connector's static headers (sent on every request) never
    carry a card number, CVV, password, or payment token."""
    connector = FujiStorePurchaseConnector()

    forbidden = ("card", "cvv", "password", "secret", "authorization")
    for key, value in connector._headers.items():
        assert not any(bad in key.lower() or bad in value.lower() for bad in forbidden)


# --- full attempt_purchase() pipeline with the real connector -----------


def test_full_pipeline_reaches_human_action_required_for_payment(
    monkeypatch: pytest.MonkeyPatch, session
) -> None:
    """End to end: a real ALLOW-worthy WatchRule + the real
    FujiStorePurchaseConnector (mocked HTTP) gets all the way through
    revalidation to a confirmed total, then correctly stops at
    HUMAN_ACTION_REQUIRED instead of ever attempting PayPal payment."""
    import asyncio

    from database import crud
    from products.matcher import MatchResult
    from products.observation import ProductObservation
    from purchase.config import PurchasePolicy
    from purchase.engine import attempt_purchase
    from purchase.models import PurchaseStatus
    from purchase.registry import PurchaseConnectorRegistry

    product = crud.create_product(session, "ALAKAZAM HOLO 1/102")
    merchant = crud.create_merchant(session, "Fuji Store")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://fuji-store.fr/produit/alakazam-holo-1-102-set-de-base-pokemon-fr-1999/",
        external_id="alakazam-holo-1-102-set-de-base-pokemon-fr-1999",
    )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=60,
        max_quantity=1,
        max_price=Decimal("100"),
    )

    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if "/products" in url:
            return _product_response(url)
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            return _cart_response(url, method="POST")
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(httpx, "request", fake_request)

    class FakeNotifier:
        def __init__(self) -> None:
            self.sent_embeds: list[object] = []

        async def send_embed(self, embed: object) -> None:
            self.sent_embeds.append(embed)

    notifier = FakeNotifier()
    observation = ProductObservation(
        merchant="Fuji Store",
        external_id="alakazam-holo-1-102-set-de-base-pokemon-fr-1999",
        name="ALAKAZAM HOLO 1/102",
        price=Decimal("89.90"),
        currency="EUR",
        available=True,
        url=listing.url,
        observed_at=datetime.now(UTC),
    )
    match = MatchResult(matched=True, confidence=100, method="ean_exact", reason="test")
    policy = PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({"fuji-store.fr"}),
        cooldown_seconds=0,
    )
    registry = PurchaseConnectorRegistry()
    registry.register("Fuji Store", FujiStorePurchaseConnector())

    outcome = asyncio.run(
        attempt_purchase(
            session, rule, observation, match, policy, registry, ("fuji-store.fr",), notifier
        )
    )

    assert outcome.status == PurchaseStatus.HUMAN_ACTION_REQUIRED
    attempts = crud.list_purchase_attempts(session)
    assert attempts[0].status == "human_action_required"
    assert "🟠 HUMAN ACTION REQUIRED" in [e.title for e in notifier.sent_embeds]


# --- Phase 34: persistent-client injection -----------------------------


def test_connector_uses_provided_client_instead_of_httpx_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent client passed at construction must actually carry
    every request — never silently fall back to a fresh
    httpx.request() connection per call."""

    def fail_if_called(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("httpx.request must not be called when a client is provided")

    monkeypatch.setattr(httpx, "request", fail_if_called)

    calls: list[str] = []

    def router(method: str, url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        if "/products" in url:
            return _product_response(url)
        if url.endswith("/cart") and method == "GET":
            return _empty_cart_get_response(url)
        if url.endswith("/cart/add-item"):
            return _cart_response(url, method="POST")
        raise AssertionError(f"unexpected request: {method} {url}")

    class _FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            return router(method, url, **kwargs)

    connector = FujiStorePurchaseConnector(client=_FakeClient())

    result = connector.revalidate(_intent())

    assert result.available is True
    assert len(calls) >= 3


def test_warm_up_makes_one_read_only_products_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "request", lambda *a, **kw: (_ for _ in ()).throw(AssertionError()))
    calls: list[tuple[str, str]] = []

    class _FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            calls.append((method, url))
            return _product_response(url, has_options=False)

    connector = FujiStorePurchaseConnector(client=_FakeClient())

    connector.warm_up()

    assert len(calls) == 1
    assert calls[0][0] == "GET"
    assert "/products" in calls[0][1]


def test_warm_up_never_raises_when_merchant_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = FujiStorePurchaseConnector()

    connector.warm_up()  # must not raise
