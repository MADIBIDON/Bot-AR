"""Single place defining which merchants have a working PurchaseConnector.

Phase 20 recon result:

- Kairyu and RelicTCG (both Shopify) publish a Universal Commerce
  Protocol (UCP — https://ucp.dev) merchant profile at /.well-known/ucp,
  Shopify's own sanctioned agent-commerce API: official MCP tools for
  cart/checkout (create_cart, create_checkout, get_checkout, ...), and
  complete_checkout explicitly requires buyer-approved payment — no
  scraping, no anti-bot bypass, and it maps directly onto this project's
  HUMAN_ACTION_REQUIRED-before-payment gate. But every UCP tool call
  requires the calling agent to publish its own reachable
  application/json profile document (no redirects) at a stable HTTPS
  URL — a one-time external hosting step this project has no
  infrastructure for yet (analogous to needing a registered eBay
  developer app or Discord bot token). Until that exists, Kairyu/RelicTCG
  stay on UnsupportedPurchaseConnector — the best future candidate, not
  a dead end.
- Fuji Store (WooCommerce) exposes the official, public, unauthenticated
  WooCommerce Store API (/wp-json/wc/store/v1/*) — the same API its own
  block-based cart UI calls. No account, no CAPTCHA, no Cloudflare
  challenge encountered: add-to-cart, shipping-rate lookup, and a real
  total (item + shipping + tax) all work today. See
  purchase/merchants/fuji_store.py — this is the first real
  PurchaseConnector. Its checkout() always raises
  HumanActionRequiredError: Fuji Store's only payment methods are PayPal
  Commerce Platform gateways, and completing either requires a human
  PayPal approval or card entry this project never automates.

Adding a real connector for Kairyu/RelicTCG later means registering it
here once UCP agent-profile hosting exists; nothing else in the pipeline
would need to change.
"""

from __future__ import annotations

from connectors.defaults import MERCHANTS
from purchase.merchants.fuji_store import FujiStorePurchaseConnector
from purchase.merchants.unsupported import UnsupportedPurchaseConnector
from purchase.registry import PurchaseConnectorRegistry

_REAL_CONNECTORS = {
    "Fuji Store": FujiStorePurchaseConnector,
}


def build_default_purchase_registry() -> PurchaseConnectorRegistry:
    registry = PurchaseConnectorRegistry()
    for merchant in MERCHANTS:
        real_connector_cls = _REAL_CONNECTORS.get(merchant.name)
        if real_connector_cls is not None:
            registry.register(merchant.name, real_connector_cls())
        else:
            registry.register(
                merchant.name, UnsupportedPurchaseConnector(merchant_name=merchant.name)
            )
    return registry
