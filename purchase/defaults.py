"""Single place defining which merchants have a working PurchaseConnector.

Phase 20/21 recon result:

- Kairyu and RelicTCG (both Shopify) publish a Universal Commerce
  Protocol (UCP — https://ucp.dev) merchant profile at /.well-known/ucp,
  Shopify's own sanctioned agent-commerce API: official MCP tools for
  cart/checkout (search_catalog, create_checkout, update_checkout,
  get_checkout, ...), and complete_checkout explicitly requires
  buyer-approved payment — no scraping, no anti-bot bypass, and it maps
  directly onto this project's HUMAN_ACTION_REQUIRED-before-payment gate.
  Every UCP tool call requires the calling agent to publish its own
  reachable application/json profile document (no redirects, and
  Cache-Control must include `public` with max-age >= 60 — GitHub Pages'
  default Cache-Control lacks `public`, so this project's profile is
  hosted on Vercel instead, see docs/UCP_HOSTING.md) at a stable HTTPS
  URL declared in PURCHASE_UCP_AGENT_PROFILE_URL. That profile's own
  `capabilities` registry must also list the same capability names the
  merchant advertises (confirmed live: an empty registry made every
  tools/call fail with "Tool not found", even for tools the merchant's
  own tools/list showed) — see ucp/profile.py. See
  purchase/merchants/shopify_ucp.py for the connector this unlocks.
- Fuji Store (WooCommerce) exposes the official, public, unauthenticated
  WooCommerce Store API (/wp-json/wc/store/v1/*) — the same API its own
  block-based cart UI calls. See purchase/merchants/fuji_store.py.

Both real connectors' checkout() always raises HumanActionRequiredError:
Fuji Store only offers PayPal Commerce Platform gateways, and Shopify UCP
checkout requires a buyer-approved payment instrument — neither is ever
constructed by this project.

Kairyu/RelicTCG fall back to UnsupportedPurchaseConnector if
PURCHASE_UCP_AGENT_PROFILE_URL is not set — never crash, never guess a
profile URL.
"""

from __future__ import annotations

import os

from connectors.defaults import MERCHANTS
from purchase.merchants.fuji_store import FujiStorePurchaseConnector
from purchase.merchants.shopify_ucp import ShopifyUCPPurchaseConnector
from purchase.merchants.unsupported import UnsupportedPurchaseConnector
from purchase.registry import PurchaseConnectorRegistry

_UCP_SHOP_DOMAINS = {
    "Kairyu": "kairyu.fr",
    "RelicTCG": "www.relictcg.com",
}


def build_default_purchase_registry() -> PurchaseConnectorRegistry:
    registry = PurchaseConnectorRegistry()
    agent_profile_url = os.environ.get("PURCHASE_UCP_AGENT_PROFILE_URL", "").strip()

    for merchant in MERCHANTS:
        if merchant.name == "Fuji Store":
            registry.register(merchant.name, FujiStorePurchaseConnector())
        elif merchant.name in _UCP_SHOP_DOMAINS and agent_profile_url:
            registry.register(
                merchant.name,
                ShopifyUCPPurchaseConnector(
                    shop_domain=_UCP_SHOP_DOMAINS[merchant.name],
                    agent_profile_url=agent_profile_url,
                ),
            )
        else:
            registry.register(
                merchant.name, UnsupportedPurchaseConnector(merchant_name=merchant.name)
            )
    return registry
