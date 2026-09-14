"""Thin UCP client: business-profile discovery + MCP JSON-RPC tool calls.

Confirmed live against Kairyu and RelicTCG (Phase 20 recon, read-only,
no purchase):
  - GET https://<shop_domain>/.well-known/ucp returns a business profile
    naming the MCP transport endpoint (services["dev.ucp.shopping"]).
  - POST <mcp_endpoint> with a JSON-RPC "tools/list" request returns the
    real tool schemas (search_catalog, create_cart, create_checkout,
    get_checkout, update_checkout, complete_checkout, ...).
  - Calling any tool without a reachable `meta.ucp-agent.profile` URL
    fails immediately with JSON-RPC error code -32001,
    data.code == "profile_unreachable" — this is the exact error this
    module's ucp-agent-profile hosting is meant to clear.

NOT yet confirmed live (blocked on profile hosting, see
ucp/profile.py): the exact shape of a SUCCESSFUL tools/call response.
MCP's own spec wraps tool results in a `content` block array, sometimes
alongside a `structuredContent` object carrying the same data typed. This
client accepts either — see _parse_tool_result() — and raises UCPError
rather than guessing if neither is present, so a shape mismatch is
reported clearly instead of silently returning wrong data.

No anti-bot bypass: a single JSON HTTP request per call, honest
User-Agent, respects 429 by raising (caller decides whether to retry).
"""

from __future__ import annotations

import json
import uuid

import httpx

from ucp.models import UCPDiscovery

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+UCP shopping client; no automated payment)"

_SHOPPING_SERVICE = "dev.ucp.shopping"


class UCPError(Exception):
    """Base class for every UCP-path failure. Never includes a secret."""


class UCPProfileUnreachableError(UCPError):
    """The merchant could not fetch our agent profile — see
    ucp/profile.py and the repo's UCP hosting setup. Not a bug in the
    merchant or in this client; it means the profile isn't hosted (yet)
    or isn't reachable from Shopify's infrastructure."""


class UCPServiceUnavailableError(UCPError):
    """The merchant doesn't expose a UCP shopping/MCP service at all, or
    discovery failed outright."""


class UCPToolCallError(UCPError):
    """A JSON-RPC error from a merchant's MCP endpoint, other than
    profile_unreachable — e.g. an invalid cart/checkout id, a genuine
    rate limit, or a malformed request."""

    def __init__(self, message: str, *, code: int | None = None, kind: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


def _request(
    method: str,
    url: str,
    *,
    timeout: float,
    client: httpx.Client | None = None,
    **kwargs: object,
) -> httpx.Response:
    """client=None (the default) preserves the exact per-call
    httpx.request() behavior this module's whole test suite monkeypatches
    directly. Phase 34: real purchase connectors pass their own
    persistent, connection-pooled httpx.Client (built once at worker
    startup, see purchase/defaults.py) to avoid paying a fresh TCP+TLS
    handshake on every single discover()/call_tool() — measured at
    ~90ms+ per call in Phase 33 against a real HTTPS endpoint."""
    headers = {"User-Agent": DEFAULT_USER_AGENT, **kwargs.pop("headers", {})}  # type: ignore[arg-type]
    send = client.request if client is not None else httpx.request
    try:
        return send(method, url, timeout=timeout, headers=headers, **kwargs)
    except httpx.TimeoutException as exc:
        raise UCPError(f"timeout calling {url}") from exc
    except httpx.RequestError as exc:
        raise UCPError(f"network error calling {url}: {exc}") from exc


def discover(
    shop_domain: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> UCPDiscovery:
    """GET https://<shop_domain>/.well-known/ucp — the business profile.
    Raises UCPServiceUnavailableError if the store doesn't publish one or
    doesn't expose an MCP shopping transport."""
    response = _request(
        "GET", f"https://{shop_domain}/.well-known/ucp", timeout=timeout, client=client
    )
    if response.status_code == 404:
        raise UCPServiceUnavailableError(f"{shop_domain} does not publish a UCP profile.")
    if response.status_code >= 400:
        raise UCPServiceUnavailableError(
            f"{shop_domain} UCP discovery returned HTTP {response.status_code}."
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise UCPServiceUnavailableError(f"{shop_domain} UCP profile is not valid JSON.") from exc

    ucp = data.get("ucp", {})
    version = ucp.get("version")
    services = ucp.get("services", {}).get(_SHOPPING_SERVICE, [])
    mcp_endpoint = next(
        (s["endpoint"] for s in services if s.get("transport") == "mcp" and "endpoint" in s), None
    )
    if not version or not mcp_endpoint:
        raise UCPServiceUnavailableError(
            f"{shop_domain} UCP profile has no usable MCP shopping endpoint."
        )
    return UCPDiscovery(version=version, mcp_endpoint=mcp_endpoint)


def _classify_error(error: dict) -> None:
    code = error.get("code")
    message = error.get("message", "UCP tool call failed")
    # `data` is a free-form JSON-RPC field: Shopify's UCP implementation
    # uses a dict for structured errors (profile_unreachable, ...) but a
    # plain string for others (confirmed live: "Tool not found: <tool>").
    data = error.get("data")
    data_dict = data if isinstance(data, dict) else {}
    kind = data_dict.get("code")
    detail = data if isinstance(data, str) else data_dict.get("content", "no detail given")
    if kind == "profile_unreachable":
        raise UCPProfileUnreachableError(
            f"{message}: the agent profile URL could not be fetched by the merchant ({detail})."
        )
    raise UCPToolCallError(f"{message}: {detail}" if data else message, code=code, kind=kind)


def _parse_tool_result(payload: dict) -> dict:
    """Best-effort per the MCP spec — NOT yet confirmed against a real
    successful Shopify UCP response (see module docstring). Tries
    structuredContent first, then a single JSON text content block."""
    result = payload.get("result")
    if result is None:
        raise UCPError("UCP tool call returned no result and no error.")
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    content = result.get("content")
    if isinstance(content, list) and len(content) == 1 and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except (ValueError, KeyError) as exc:
            raise UCPError("UCP tool call returned unparseable text content.") from exc
    raise UCPError(
        "UCP tool call succeeded but returned a response shape this client doesn't "
        "recognize yet — see ucp/client.py's module docstring."
    )


def call_tool(
    mcp_endpoint: str,
    tool_name: str,
    arguments: dict,
    *,
    agent_profile_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> dict:
    """Calls one UCP/MCP shopping tool (search_catalog, create_cart,
    create_checkout, get_checkout, update_checkout, complete_checkout,
    ...) and returns its parsed result. Raises UCPProfileUnreachableError
    if the merchant can't fetch agent_profile_url, UCPToolCallError for
    any other JSON-RPC error, UCPError for a transport/shape problem.
    A future Shopify UCP PurchaseConnector must never call complete_checkout
    with a real payment instrument.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {
                **arguments,
                "meta": {"ucp-agent": {"profile": agent_profile_url}},
            },
        },
    }
    response = _request(
        "POST",
        mcp_endpoint,
        timeout=timeout,
        headers={"Content-Type": "application/json"},
        json=payload,
        client=client,
    )
    if response.status_code == 429:
        raise UCPToolCallError("Rate limited (429) by the merchant's UCP/MCP endpoint.", code=429)
    if response.status_code >= 500:
        raise UCPError(f"UCP/MCP endpoint returned HTTP {response.status_code}.")
    try:
        body = response.json()
    except ValueError as exc:
        raise UCPError("UCP/MCP endpoint returned a non-JSON response.") from exc

    if "error" in body:
        _classify_error(body["error"])
    return _parse_tool_result(body)
