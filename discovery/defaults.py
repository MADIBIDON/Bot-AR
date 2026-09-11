"""Single place defining which merchants have a working discovery source.

Kairyu/RelicTCG: Shopify UCP `search_catalog`, the same capability
already confirmed live and used by purchase/merchants/shopify_ucp.py —
one discovery implementation for both, no per-merchant scraping. Both use
config.settings.get_ucp_agent_profile_url() (Phase 23), which defaults to
this project's own public, always-on UCP profile — no env var needs to be
set for these to work; PURCHASE_UCP_AGENT_PROFILE_URL still overrides it
when set. If that ever resolves to an unreachable URL, both are simply
not registered, and app/discovery.py reports DISCOVERY_UNAVAILABLE for
them without touching the other merchants.

Fuji Store: WooCommerce's public Store API search (`?search=`), no
configuration needed.
"""

from __future__ import annotations

from config.settings import get_ucp_agent_profile_url
from connectors.defaults import MERCHANTS
from discovery.registry import DiscoveryRegistry
from discovery.shopify_ucp import ShopifyUCPDiscoverySource
from discovery.woocommerce import WooCommerceDiscoverySource

_UCP_SHOP_DOMAINS = {
    "Kairyu": "kairyu.fr",
    "RelicTCG": "www.relictcg.com",
}
_WOOCOMMERCE_SHOP_DOMAINS = {
    "Fuji Store": "fuji-store.fr",
}


def build_default_discovery_registry() -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    agent_profile_url = get_ucp_agent_profile_url()

    for merchant in MERCHANTS:
        if merchant.name in _UCP_SHOP_DOMAINS and agent_profile_url:
            registry.register(
                merchant.name,
                ShopifyUCPDiscoverySource(
                    shop_domain=_UCP_SHOP_DOMAINS[merchant.name],
                    merchant_name=merchant.name,
                    agent_profile_url=agent_profile_url,
                ),
            )
        elif merchant.name in _WOOCOMMERCE_SHOP_DOMAINS:
            registry.register(
                merchant.name,
                WooCommerceDiscoverySource(
                    shop_domain=_WOOCOMMERCE_SHOP_DOMAINS[merchant.name],
                    merchant_name=merchant.name,
                ),
            )
        # Merchants with neither a configured UCP profile nor a
        # WooCommerce Store API are simply not registered — the
        # orchestrator (app/discovery.py) reports DISCOVERY_UNAVAILABLE
        # for any unregistered merchant and continues with the others.
    return registry
