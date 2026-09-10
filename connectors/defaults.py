"""Single place defining which merchants exist and how they're wired up.

Used by app/main_worker.py (to build the live ConnectorRegistry) and by
scripts/watch.py (to auto-detect a merchant/connector from a product
URL's hostname). Adding a merchant means adding one MerchantDefinition
here — nothing else in the pipeline changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from connectors.base import BaseConnector
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from connectors.shopify import ShopifyConnector
from connectors.woocommerce import WooCommerceConnector


@dataclass(frozen=True)
class MerchantDefinition:
    name: str
    domains: tuple[str, ...]
    build_connector: Callable[[], BaseConnector]


MERCHANTS: tuple[MerchantDefinition, ...] = (
    MerchantDefinition(
        name="Kairyu",
        domains=("kairyu.fr", "www.kairyu.fr"),
        build_connector=lambda: ShopifyConnector(shop_domain="kairyu.fr", merchant_name="Kairyu"),
    ),
    MerchantDefinition(
        name="RelicTCG",
        domains=("relictcg.com", "www.relictcg.com"),
        build_connector=lambda: ShopifyConnector(
            shop_domain="www.relictcg.com", merchant_name="RelicTCG"
        ),
    ),
    MerchantDefinition(
        name="Fuji Store",
        domains=("fuji-store.fr", "www.fuji-store.fr"),
        build_connector=lambda: WooCommerceConnector(
            shop_domain="fuji-store.fr", merchant_name="Fuji Store"
        ),
    ),
)


def _normalize_hostname(hostname: str) -> str:
    return hostname.strip().lower().removeprefix("www.")


def find_merchant_for_domain(hostname: str) -> MerchantDefinition | None:
    target = _normalize_hostname(hostname)
    for merchant in MERCHANTS:
        if target in {_normalize_hostname(domain) for domain in merchant.domains}:
            return merchant
    return None


def domains_for_merchant(name: str) -> tuple[str, ...]:
    """Reverse of find_merchant_for_domain — used by the purchase engine
    to check a WatchRule's merchant against PURCHASE_ALLOWED_MERCHANTS
    (which lists domains, not display names). Empty for an unknown
    merchant name — never guesses."""
    for merchant in MERCHANTS:
        if merchant.name == name:
            return merchant.domains
    return ()


def supported_domains_summary() -> str:
    return "\n".join(f"  {merchant.name}: {', '.join(merchant.domains)}" for merchant in MERCHANTS)


def build_default_registry() -> ConnectorRegistry:
    registry = ConnectorRegistry()
    registry.register("FakeStore", FakeStoreConnector())
    for merchant in MERCHANTS:
        registry.register(merchant.name, merchant.build_connector())
    return registry
