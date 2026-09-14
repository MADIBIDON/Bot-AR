"""Sanity check for the local real-socket test server used by the
Phase 34 warm-path benchmark — never contacts a real merchant."""

from __future__ import annotations

import httpx

from connectors.fake_merchant_http_server import FakeMerchantServer


def test_server_answers_a_real_post_over_loopback() -> None:
    with FakeMerchantServer() as server:
        response = httpx.post(f"{server.base_url}/checkout", json={"line_items": []}, timeout=5)

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_persistent_client_reuses_the_connection() -> None:
    with FakeMerchantServer() as server:
        with httpx.Client(timeout=5) as client:
            first = client.post(f"{server.base_url}/checkout", json={})
            second = client.post(f"{server.base_url}/checkout", json={})

    assert first.status_code == 200
    assert second.status_code == 200
