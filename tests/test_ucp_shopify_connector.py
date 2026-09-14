"""No real network: httpx.request (used by ucp/client.py under the hood
via httpx.request in _request()) is monkeypatched to return locally-built
httpx.Response objects shaped exactly like the REAL Kairyu/RelicTCG
responses captured live during Phase 21 (search_catalog, create_checkout,
update_checkout — see purchase/merchants/shopify_ucp.py's module
docstring for what was actually observed vs. structurally inferred).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from purchase.base import (
    AutomatedCheckoutUnsupportedError,
    HumanActionRequiredError,
    StaleListingError,
)
from purchase.merchants.shopify_ucp import ShopifyUCPPurchaseConnector
from purchase.models import PurchaseIntent

PROFILE_URL = "https://ucp-profile.vercel.app/agent-profile.json"
MCP_ENDPOINT = "https://kairyushop.myshopify.com/api/ucp/mcp"

_DISCOVERY_BODY = {
    "ucp": {
        "version": "2026-08-25",
        "services": {
            "dev.ucp.shopping": [
                {"version": "2026-08-25", "transport": "mcp", "endpoint": MCP_ENDPOINT}
            ]
        },
    }
}


def _intent(**overrides: object) -> PurchaseIntent:
    defaults: dict[str, object] = dict(
        watch_rule_id=1,
        product_id=1,
        listing_id=1,
        merchant="Kairyu",
        product_name="Elite Trainer Box ME04 - Chaos Ascendant",
        url="https://kairyu.fr/products/scelle-elite-trainer-box-me04-chaos-ascendant-fr",
        observed_price=Decimal("74.90"),
        max_price_allowed=Decimal("80"),
        quantity=1,
        match_confidence=100,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return PurchaseIntent(**defaults)  # type: ignore[arg-type]


def _rpc_result(structured_content: dict) -> dict:
    return {"jsonrpc": "2.0", "id": "1", "result": {"structuredContent": structured_content}}


def _rpc_error(code: int, message: str, data: object = None) -> dict:
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": "1", "error": error}


def _product(handle: str, variants: list[dict]) -> dict:
    return {
        "id": "gid://shopify/Product/1",
        "title": "Elite Trainer Box ME04 - Chaos Ascendant",
        "handle": handle,
        "variants": variants,
    }


def _variant(variant_id: str, price_amount: int, available: bool = True) -> dict:
    return {
        "id": variant_id,
        "sku": "SKU-1",
        "title": "Neuf",
        "price": {"amount": price_amount, "currency": "EUR"},
        "availability": {"available": available},
    }


_HANDLE = "scelle-elite-trainer-box-me04-chaos-ascendant-fr"
_VARIANT_ID = "gid://shopify/ProductVariant/57591808033103"


def _router(search_result=None, checkout_result=None, checkout_error=None, update_result=None):
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/.well-known/ucp"):
            return httpx.Response(200, json=_DISCOVERY_BODY, request=httpx.Request(method, url))
        body = kwargs.get("json", {})
        tool = body.get("params", {}).get("name")
        if tool == "search_catalog":
            return httpx.Response(200, json=search_result, request=httpx.Request(method, url))
        if tool == "create_checkout":
            if checkout_error is not None:
                return httpx.Response(200, json=checkout_error, request=httpx.Request(method, url))
            return httpx.Response(200, json=checkout_result, request=httpx.Request(method, url))
        if tool == "update_checkout":
            return httpx.Response(200, json=update_result, request=httpx.Request(method, url))
        raise AssertionError(f"unexpected tool call: {tool}")

    return fake_request


def _checkout_response(*, totals: list[dict], messages: list[dict] | None = None) -> dict:
    return _rpc_result(
        {
            "id": "gid://shopify/Checkout/abc?key=xyz",
            "currency": "EUR",
            "line_items": [],
            "totals": totals,
            "status": "incomplete",
            "messages": messages or [],
            "continue_url": "https://kairyushop.myshopify.com/",
        }
    )


def test_resolve_variant_and_create_checkout_success(monkeypatch: pytest.MonkeyPatch) -> None:
    search = _rpc_result(
        {
            "products": [_product(_HANDLE, [_variant(_VARIANT_ID, 7490)])],
            "pagination": {"has_next_page": False},
            "messages": [],
        }
    )
    checkout = _checkout_response(
        totals=[
            {"type": "subtotal", "amount": 7490, "display_text": "Subtotal"},
            {"type": "total", "amount": 7490, "display_text": "Total"},
        ]
    )
    monkeypatch.setattr(httpx, "request", _router(search_result=search, checkout_result=checkout))

    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL)
    result = connector.revalidate(_intent())

    assert result.available is True
    assert result.price == Decimal("74.90")
    assert result.shipping_cost is None  # no PURCHASE_SHIPPING_POSTAL_CODE/CONTACT_EMAIL set


def test_url_with_variant_query_string_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 33 P0 fix: every real Kairyu/RelicTCG Listing this project
    has ever discovered (discovery/shopify_ucp.py's own real candidates)
    carries a "?variant=<id>" suffix on its URL — the handle extraction
    used to swallow that whole query string into the handle, so it could
    never match a real catalog handle, and every one of those real,
    valid listings would have failed revalidate() with a false
    StaleListingError."""
    search = _rpc_result(
        {
            "products": [_product(_HANDLE, [_variant(_VARIANT_ID, 7490)])],
            "pagination": {"has_next_page": False},
            "messages": [],
        }
    )
    checkout = _checkout_response(
        totals=[
            {"type": "subtotal", "amount": 7490, "display_text": "Subtotal"},
            {"type": "total", "amount": 7490, "display_text": "Total"},
        ]
    )
    monkeypatch.setattr(httpx, "request", _router(search_result=search, checkout_result=checkout))

    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL)
    intent = _intent(
        url=(
            "https://kairyu.fr/products/scelle-elite-trainer-box-me04-chaos-ascendant-fr"
            "?variant=57591808033103"
        )
    )

    result = connector.revalidate(intent)

    assert result.available is True
    assert result.price == Decimal("74.90")


def test_shipping_and_tax_included_when_update_checkout_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PURCHASE_SHIPPING_POSTAL_CODE", "75001")
    monkeypatch.setenv("PURCHASE_CONTACT_EMAIL", "test@example.com")
    search = _rpc_result(
        {
            "products": [_product(_HANDLE, [_variant(_VARIANT_ID, 7490)])],
            "pagination": {},
            "messages": [],
        }
    )
    checkout = _checkout_response(
        totals=[{"type": "subtotal", "amount": 7490, "display_text": "Subtotal"}]
    )
    updated = _checkout_response(
        totals=[
            {"type": "subtotal", "amount": 7490, "display_text": "Subtotal"},
            {"type": "fulfillment", "amount": 899, "display_text": "Shipping"},
            {"type": "tax", "amount": 332, "display_text": "Federal Tax"},
            {"type": "tax", "amount": 465, "display_text": "State Tax"},
        ]
    )
    monkeypatch.setattr(
        httpx,
        "request",
        _router(search_result=search, checkout_result=checkout, update_result=updated),
    )

    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL)
    result = connector.revalidate(_intent())

    assert result.price == Decimal("74.90")
    assert result.shipping_cost == Decimal("8.99")
    assert result.tax_amount == Decimal("7.97")  # 3.32 + 4.65


def test_no_delivery_available_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PURCHASE_SHIPPING_POSTAL_CODE", "75001")
    monkeypatch.setenv("PURCHASE_CONTACT_EMAIL", "test@example.com")
    search = _rpc_result(
        {
            "products": [_product(_HANDLE, [_variant(_VARIANT_ID, 7490)])],
            "pagination": {},
            "messages": [],
        }
    )
    checkout = _checkout_response(
        totals=[{"type": "subtotal", "amount": 7490, "display_text": "Subtotal"}]
    )
    updated = _checkout_response(
        totals=[{"type": "subtotal", "amount": 7490, "display_text": "Subtotal"}],
        messages=[
            {
                "type": "error",
                "content_type": "plain",
                "code": "delivery_no_delivery_available",
                "content": "cannot be shipped",
                "severity": "recoverable",
            }
        ],
    )
    monkeypatch.setattr(
        httpx,
        "request",
        _router(search_result=search, checkout_result=checkout, update_result=updated),
    )

    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL)
    result = connector.revalidate(_intent())

    assert result.available is False


def test_product_not_found_in_search_raises_stale_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    search = _rpc_result({"products": [], "pagination": {}, "messages": []})
    monkeypatch.setattr(httpx, "request", _router(search_result=search))

    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL)

    with pytest.raises(StaleListingError):
        connector.revalidate(_intent())


def test_multiple_variants_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    search = _rpc_result(
        {
            "products": [
                _product(_HANDLE, [_variant(_VARIANT_ID, 7490), _variant("gid://x/2", 6490)])
            ],
            "pagination": {},
            "messages": [],
        }
    )
    monkeypatch.setattr(httpx, "request", _router(search_result=search))

    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL)

    with pytest.raises(AutomatedCheckoutUnsupportedError, match="variants"):
        connector.revalidate(_intent())


def test_variant_unavailable_reports_out_of_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    search = _rpc_result(
        {
            "products": [_product(_HANDLE, [_variant(_VARIANT_ID, 7490, available=False)])],
            "pagination": {},
            "messages": [],
        }
    )
    monkeypatch.setattr(httpx, "request", _router(search_result=search))

    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL)
    result = connector.revalidate(_intent())

    assert result.available is False
    assert result.quantity_available == 0


def test_tool_not_found_error_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact live error hit before capabilities were declared —
    must surface as a clear PurchaseError, never crash silently."""
    from purchase.base import PurchaseError

    search = _rpc_error(-32602, "Invalid params", "Tool not found: search_catalog")
    monkeypatch.setattr(httpx, "request", _router(search_result=search))

    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL)

    with pytest.raises(PurchaseError, match="Tool not found"):
        connector.revalidate(_intent())


def test_missing_agent_profile_url_raises_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 23: get_ucp_agent_profile_url() always resolves to this
    project's own hosted default now (no env var required), so the only
    way left to hit a genuinely empty URL is if that resolution itself
    ever returned "" — kept as a defense-in-depth guard against exactly
    that, verified here by forcing it directly rather than via env."""
    from purchase.base import PurchaseError

    monkeypatch.setattr("purchase.merchants.shopify_ucp.get_ucp_agent_profile_url", lambda: "")
    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=None)

    with pytest.raises(PurchaseError, match="PURCHASE_UCP_AGENT_PROFILE_URL"):
        connector.revalidate(_intent())


def test_merchant_without_ucp_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = ShopifyUCPPurchaseConnector(
        shop_domain="no-ucp.example", agent_profile_url=PROFILE_URL
    )

    with pytest.raises(AutomatedCheckoutUnsupportedError):
        connector.revalidate(_intent())


def test_checkout_always_requires_human_action() -> None:
    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL)

    with pytest.raises(HumanActionRequiredError, match="payment instrument"):
        connector.checkout(_intent(), revalidated=None)  # type: ignore[arg-type]


# --- full attempt_purchase() pipeline with the real connector -----------


def test_full_pipeline_reaches_human_action_required(
    monkeypatch: pytest.MonkeyPatch, session
) -> None:
    """End to end: a real ALLOW-worthy WatchRule + the real
    ShopifyUCPPurchaseConnector (mocked HTTP) resolves the product,
    creates a real-shaped checkout with a confirmed total within budget,
    then correctly stops at HUMAN_ACTION_REQUIRED instead of ever
    attempting payment."""
    import asyncio

    from database import crud
    from products.matcher import MatchResult
    from products.observation import ProductObservation
    from purchase.config import PurchasePolicy
    from purchase.engine import attempt_purchase
    from purchase.models import PurchaseStatus
    from purchase.registry import PurchaseConnectorRegistry

    product = crud.create_product(session, "Elite Trainer Box ME04 - Chaos Ascendant")
    merchant = crud.create_merchant(session, "Kairyu")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url=f"https://kairyu.fr/products/{_HANDLE}",
        external_id=_HANDLE,
    )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=60,
        max_quantity=1,
        max_price=Decimal("80"),
    )

    search = _rpc_result(
        {
            "products": [_product(_HANDLE, [_variant(_VARIANT_ID, 7490)])],
            "pagination": {},
            "messages": [],
        }
    )
    checkout = _checkout_response(
        totals=[
            {"type": "subtotal", "amount": 7490, "display_text": "Subtotal"},
            {"type": "total", "amount": 7490, "display_text": "Total"},
        ]
    )
    monkeypatch.setattr(httpx, "request", _router(search_result=search, checkout_result=checkout))

    class FakeNotifier:
        def __init__(self) -> None:
            self.sent_embeds: list[object] = []

        async def send_embed(self, embed: object) -> None:
            self.sent_embeds.append(embed)

    notifier = FakeNotifier()
    observation = ProductObservation(
        merchant="Kairyu",
        external_id=_HANDLE,
        name="Elite Trainer Box ME04 - Chaos Ascendant",
        price=Decimal("74.90"),
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
        allowed_merchant_domains=frozenset({"kairyu.fr"}),
        cooldown_seconds=0,
    )
    registry = PurchaseConnectorRegistry()
    registry.register(
        "Kairyu",
        ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL),
    )

    outcome = asyncio.run(
        attempt_purchase(
            session, rule, observation, match, policy, registry, ("kairyu.fr",), notifier
        )
    )

    assert outcome.status == PurchaseStatus.HUMAN_ACTION_REQUIRED
    attempts = crud.list_purchase_attempts(session)
    assert attempts[0].status == "human_action_required"
    assert "HUMAN ACTION REQUIRED" in [e.title for e in notifier.sent_embeds]


def test_full_pipeline_cancels_when_total_exceeds_max_price(
    monkeypatch: pytest.MonkeyPatch, session
) -> None:
    """final_total <= max_price is re-checked against the revalidated
    (post-checkout-creation) total, not just the pre-check estimate."""
    import asyncio

    from database import crud
    from products.matcher import MatchResult
    from products.observation import ProductObservation
    from purchase.config import PurchasePolicy
    from purchase.engine import attempt_purchase
    from purchase.models import PurchaseStatus
    from purchase.registry import PurchaseConnectorRegistry

    product = crud.create_product(session, "Elite Trainer Box ME04 - Chaos Ascendant")
    merchant = crud.create_merchant(session, "Kairyu")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url=f"https://kairyu.fr/products/{_HANDLE}",
        external_id=_HANDLE,
    )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=60,
        max_quantity=1,
        max_price=Decimal("80"),  # rule's own ceiling passes the pre-check (74.90 <= 80)
    )

    search = _rpc_result(
        {
            "products": [_product(_HANDLE, [_variant(_VARIANT_ID, 7490)])],
            "pagination": {},
            "messages": [],
        }
    )
    # Revalidated checkout reveals shipping that pushes the real total over 80.
    checkout = _checkout_response(
        totals=[
            {"type": "subtotal", "amount": 7490, "display_text": "Subtotal"},
            {"type": "fulfillment", "amount": 900, "display_text": "Shipping"},
        ]
    )
    monkeypatch.setattr(
        httpx,
        "request",
        _router(
            search_result=search,
            checkout_result=_checkout_response(
                totals=[{"type": "subtotal", "amount": 7490, "display_text": "Subtotal"}]
            ),
            update_result=checkout,
        ),
    )
    monkeypatch.setenv("PURCHASE_SHIPPING_POSTAL_CODE", "75001")
    monkeypatch.setenv("PURCHASE_CONTACT_EMAIL", "test@example.com")

    class FakeNotifier:
        def __init__(self) -> None:
            self.sent_embeds: list[object] = []

        async def send_embed(self, embed: object) -> None:
            self.sent_embeds.append(embed)

    notifier = FakeNotifier()
    observation = ProductObservation(
        merchant="Kairyu",
        external_id=_HANDLE,
        name="Elite Trainer Box ME04 - Chaos Ascendant",
        price=Decimal("74.90"),
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
        allowed_merchant_domains=frozenset({"kairyu.fr"}),
        cooldown_seconds=0,
    )
    registry = PurchaseConnectorRegistry()
    registry.register(
        "Kairyu",
        ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL),
    )

    outcome = asyncio.run(
        attempt_purchase(
            session, rule, observation, match, policy, registry, ("kairyu.fr",), notifier
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED


# --- Phase 34: persistent-client injection -----------------------------


def test_connector_uses_provided_client_for_discover_and_call_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent client passed at construction must actually carry
    both the one-time discover() and every call_tool() — never silently
    fall back to a fresh httpx.request() connection per call."""

    def fail_if_called(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("httpx.request must not be called when a client is provided")

    monkeypatch.setattr(httpx, "request", fail_if_called)

    search = _rpc_result({"products": [_product(_HANDLE, [_variant(_VARIANT_ID, 7490)])]})
    checkout = _checkout_response(totals=[{"type": "subtotal", "amount": 7490}])
    router = _router(search_result=search, checkout_result=checkout)

    calls: list[str] = []

    class _FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            calls.append(url)
            return router(method, url, **kwargs)

    fake_client = _FakeClient()
    connector = ShopifyUCPPurchaseConnector(
        shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL, client=fake_client
    )

    connector.revalidate(_intent())

    assert calls[0] == "https://kairyu.fr/.well-known/ucp"
    assert any(c == MCP_ENDPOINT for c in calls[1:])
    # Second call must reuse the cached mcp_endpoint (no second discover).
    connector.revalidate(_intent())
    assert calls.count("https://kairyu.fr/.well-known/ucp") == 1


def test_warm_up_resolves_the_endpoint_via_the_provided_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "request", lambda *a, **kw: (_ for _ in ()).throw(AssertionError()))
    calls: list[str] = []

    class _FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            calls.append(url)
            return httpx.Response(200, json=_DISCOVERY_BODY, request=httpx.Request(method, url))

    connector = ShopifyUCPPurchaseConnector(
        shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL, client=_FakeClient()
    )

    connector.warm_up()

    assert calls == ["https://kairyu.fr/.well-known/ucp"]
    # Second warm_up (or a real revalidate right after) must not re-discover.
    connector.warm_up()
    assert calls == ["https://kairyu.fr/.well-known/ucp"]


def test_warm_up_never_raises_when_merchant_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(503, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    connector = ShopifyUCPPurchaseConnector(shop_domain="kairyu.fr", agent_profile_url=PROFILE_URL)

    connector.warm_up()  # must not raise
