"""No real network for the failure/shape-parsing paths (httpx.request is
monkeypatched); the two success paths for discover() are also exercised
here with mocks matching the real Kairyu/RelicTCG responses captured
during Phase 20/21 recon (see ucp/client.py's module docstring for what
was actually confirmed live vs. best-effort).
"""

from __future__ import annotations

import json

import httpx
import pytest

from ucp.client import (
    UCPError,
    UCPProfileUnreachableError,
    UCPServiceUnavailableError,
    UCPToolCallError,
    call_tool,
    discover,
)

_REAL_KAIRYU_UCP_BODY = {
    "ucp": {
        "version": "2026-08-25",
        "services": {
            "dev.ucp.shopping": [
                {
                    "version": "2026-08-25",
                    "transport": "mcp",
                    "endpoint": "https://kairyushop.myshopify.com/api/ucp/mcp",
                },
                {"version": "2026-04-08", "transport": "embedded"},
            ]
        },
    }
}


def test_discover_parses_real_kairyu_shaped_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=_REAL_KAIRYU_UCP_BODY, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)

    result = discover("kairyu.fr")

    assert result.version == "2026-08-25"
    assert result.mcp_endpoint == "https://kairyushop.myshopify.com/api/ucp/mcp"


def test_discover_404_raises_service_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPServiceUnavailableError):
        discover("not-a-ucp-store.example")


def test_discover_no_mcp_transport_raises_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        body = {"ucp": {"version": "2026-08-25", "services": {}}}
        return httpx.Response(200, json=body, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPServiceUnavailableError):
        discover("kairyu.fr")


def test_discover_malformed_json_raises_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, text="not json", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPServiceUnavailableError):
        discover("kairyu.fr")


def test_discover_timeout_raises_ucp_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPError, match="timeout"):
        discover("kairyu.fr")


# --- call_tool --------------------------------------------------------


def _rpc_response(url: str, payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("POST", url))


def test_call_tool_profile_unreachable_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return _rpc_response(
            url,
            {
                "jsonrpc": "2.0",
                "id": "1",
                "error": {
                    "code": -32001,
                    "message": "UCP discovery failed",
                    "data": {"code": "profile_unreachable", "content": "Network error"},
                },
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPProfileUnreachableError):
        call_tool(
            "https://kairyushop.myshopify.com/api/ucp/mcp",
            "search_catalog",
            {"catalog": {"query": "test"}},
            agent_profile_url="https://example.invalid/agent-profile.json",
        )


def test_call_tool_generic_json_rpc_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return _rpc_response(
            url,
            {
                "jsonrpc": "2.0",
                "id": "1",
                "error": {"code": -32602, "message": "Invalid params"},
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPToolCallError, match="Invalid params"):
        call_tool(
            "https://kairyushop.myshopify.com/api/ucp/mcp",
            "create_cart",
            {"cart": {}},
            agent_profile_url="https://example.com/profile.json",
        )


def test_call_tool_error_with_string_data_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the real "Tool not found" error observed live from
    Kairyu/RelicTCG uses a plain string for `data`
    ({"code": -32602, "data": "Tool not found: search_catalog"}), not a
    dict — _classify_error() used to call data.get() unconditionally and
    crashed with AttributeError on this exact real shape."""

    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return _rpc_response(
            url,
            {
                "jsonrpc": "2.0",
                "id": "1",
                "error": {
                    "code": -32602,
                    "message": "Invalid params",
                    "data": "Tool not found: search_catalog",
                },
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPToolCallError, match="Tool not found: search_catalog"):
        call_tool(
            "https://kairyushop.myshopify.com/api/ucp/mcp",
            "search_catalog",
            {"catalog": {"query": "x"}},
            agent_profile_url="https://example.com/profile.json",
        )


def test_call_tool_success_with_structured_content(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return _rpc_response(
            url,
            {
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"structuredContent": {"id": "gid://shopify/Cart/1", "totals": {}}},
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)

    result = call_tool(
        "https://kairyushop.myshopify.com/api/ucp/mcp",
        "create_cart",
        {"cart": {"line_items": []}},
        agent_profile_url="https://example.com/profile.json",
    )

    assert result["id"] == "gid://shopify/Cart/1"


def test_call_tool_success_with_text_content_block(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return _rpc_response(
            url,
            {
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"content": [{"type": "text", "text": json.dumps({"id": "gid://x/1"})}]},
            },
        )

    monkeypatch.setattr(httpx, "request", fake_request)

    result = call_tool(
        "https://kairyushop.myshopify.com/api/ucp/mcp",
        "get_checkout",
        {"id": "gid://x/1"},
        agent_profile_url="https://example.com/profile.json",
    )

    assert result["id"] == "gid://x/1"


def test_call_tool_unrecognized_result_shape_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return _rpc_response(url, {"jsonrpc": "2.0", "id": "1", "result": {}})

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPError, match="response shape"):
        call_tool(
            "https://kairyushop.myshopify.com/api/ucp/mcp",
            "get_checkout",
            {},
            agent_profile_url="https://example.com/profile.json",
        )


def test_call_tool_no_result_and_no_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return _rpc_response(url, {"jsonrpc": "2.0", "id": "1"})

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPError):
        call_tool(
            "https://kairyushop.myshopify.com/api/ucp/mcp",
            "get_checkout",
            {},
            agent_profile_url="https://example.com/profile.json",
        )


def test_call_tool_rate_limited_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(429, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPToolCallError, match="429"):
        call_tool(
            "https://kairyushop.myshopify.com/api/ucp/mcp",
            "search_catalog",
            {"catalog": {"query": "x"}},
            agent_profile_url="https://example.com/profile.json",
        )


def test_call_tool_server_error_raises_ucp_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPError, match="500"):
        call_tool(
            "https://kairyushop.myshopify.com/api/ucp/mcp",
            "search_catalog",
            {"catalog": {"query": "x"}},
            agent_profile_url="https://example.com/profile.json",
        )


def test_call_tool_non_json_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPError, match="non-JSON"):
        call_tool(
            "https://kairyushop.myshopify.com/api/ucp/mcp",
            "search_catalog",
            {"catalog": {"query": "x"}},
            agent_profile_url="https://example.com/profile.json",
        )


def test_call_tool_sends_agent_profile_url_in_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        captured["json"] = kwargs["json"]
        body = {"jsonrpc": "2.0", "id": "1", "result": {"structuredContent": {}}}
        return _rpc_response(url, body)

    monkeypatch.setattr(httpx, "request", fake_request)

    call_tool(
        "https://kairyushop.myshopify.com/api/ucp/mcp",
        "search_catalog",
        {"catalog": {"query": "x"}},
        agent_profile_url="https://example.com/my-profile.json",
    )

    meta = captured["json"]["params"]["arguments"]["meta"]
    assert meta["ucp-agent"]["profile"] == "https://example.com/my-profile.json"


def test_call_tool_timeout_raises_ucp_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(UCPError, match="timeout"):
        call_tool(
            "https://kairyushop.myshopify.com/api/ucp/mcp",
            "search_catalog",
            {"catalog": {"query": "x"}},
            agent_profile_url="https://example.com/profile.json",
        )


# --- Phase 34: persistent-client injection -----------------------------


def test_discover_uses_provided_client_instead_of_httpx_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-provided persistent client must actually be used for
    dispatch — never silently ignored in favor of the fresh-connection-
    per-call httpx.request() path."""

    def fail_if_called(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("httpx.request must not be called when a client is provided")

    monkeypatch.setattr(httpx, "request", fail_if_called)

    calls: list[tuple[str, str]] = []

    class _FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            calls.append((method, url))
            return httpx.Response(
                200, json=_REAL_KAIRYU_UCP_BODY, request=httpx.Request(method, url)
            )

    discover("kairyu.fr", client=_FakeClient())

    assert calls == [("GET", "https://kairyu.fr/.well-known/ucp")]


def test_call_tool_uses_provided_client_instead_of_httpx_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("httpx.request must not be called when a client is provided")

    monkeypatch.setattr(httpx, "request", fail_if_called)

    calls: list[str] = []

    class _FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            calls.append(url)
            body = {"jsonrpc": "2.0", "id": "1", "result": {"structuredContent": {}}}
            return httpx.Response(200, json=body, request=httpx.Request(method, url))

    call_tool(
        "https://kairyushop.myshopify.com/api/ucp/mcp",
        "search_catalog",
        {"catalog": {"query": "x"}},
        agent_profile_url="https://example.com/profile.json",
        client=_FakeClient(),
    )

    assert calls == ["https://kairyushop.myshopify.com/api/ucp/mcp"]


# --- Phase 35 section 22: safety regression with a persistent client ---


def test_discover_with_persistent_client_timeout_is_a_clean_ucp_error() -> None:
    """A merchant timeout must be a clean UCPError even when a real,
    persistent (Phase 34) client is used — never an unhandled crash."""

    class _TimingOutClient:
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            raise httpx.TimeoutException("timed out", request=httpx.Request(method, url))

    with pytest.raises(UCPError, match="timeout"):
        discover("kairyu.fr", client=_TimingOutClient())


def test_discover_with_persistent_client_connection_failure_is_a_clean_ucp_error() -> None:
    """Simulates the persistent client's own connection pool failing
    (e.g. the underlying socket was reset) — must still be converted to
    UCPError, never propagate as a raw httpx exception."""

    class _BrokenPoolClient:
        def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError(
                "connection pool exhausted/reset", request=httpx.Request(method, url)
            )

    with pytest.raises(UCPError, match="network error"):
        discover("kairyu.fr", client=_BrokenPoolClient())
