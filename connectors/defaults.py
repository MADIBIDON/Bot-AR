"""Single place defining which merchants exist and how they're wired up.

Used by both app/main_worker.py and scripts/watch.py so the two never
drift apart on which connectors are registered under which merchant name.
"""

from __future__ import annotations

from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from connectors.shopify import ShopifyConnector


def build_default_registry() -> ConnectorRegistry:
    registry = ConnectorRegistry()
    registry.register("FakeStore", FakeStoreConnector())
    registry.register("Kairyu", ShopifyConnector(shop_domain="kairyu.fr", merchant_name="Kairyu"))
    return registry
