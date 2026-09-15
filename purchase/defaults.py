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

Kairyu/RelicTCG fall back to UnsupportedPurchaseConnector only if
config.settings.get_ucp_agent_profile_url() somehow resolves empty (it
never does — it defaults to this project's own hosted profile, Phase 23,
PURCHASE_UCP_AGENT_PROFILE_URL still overrides it) — never crash, never
guess a profile URL.
"""

from __future__ import annotations

import httpx

from config.settings import get_ucp_agent_profile_url
from connectors.defaults import MERCHANTS
from purchase.merchants.cultura import DEFAULT_TIMEOUT_SECONDS as _CULTURA_TIMEOUT_SECONDS
from purchase.merchants.cultura import CulturaPurchaseConnector
from purchase.merchants.fuji_store import DEFAULT_TIMEOUT_SECONDS as _FUJI_TIMEOUT_SECONDS
from purchase.merchants.fuji_store import FujiStorePurchaseConnector
from purchase.merchants.shopify_ucp import ShopifyUCPPurchaseConnector
from purchase.merchants.unsupported import UnsupportedPurchaseConnector
from purchase.registry import PurchaseConnectorRegistry
from ucp.client import DEFAULT_TIMEOUT_SECONDS as _UCP_TIMEOUT_SECONDS

_UCP_SHOP_DOMAINS = {
    "Kairyu": "kairyu.fr",
    "RelicTCG": "www.relictcg.com",
}


def _persistent_client(timeout: float) -> httpx.Client:
    """Phase 34 (100ms warm-path target): one persistent, connection-
    pooled httpx.Client per real purchase connector, built once here (at
    worker startup, see app/main_worker.py) and reused for the whole
    process lifetime — extends to the purchase side the same pattern
    connectors/schema_org.py already established for monitoring (Phase
    23). Avoids paying a fresh TCP+TLS handshake (~90-150ms measured,
    Phase 33/34) on every single revalidate()/checkout() network call.
    Safe to share across the worker's threads (httpx.Client's connection
    pool is thread-safe — same justification as connectors/schema_org.py).

    keepalive_expiry=120 (httpx's own default is 5s) so a connection
    survives between two checks of the same merchant during a
    high-frequency release window (engine/release_awareness.py's
    fast/high-frequency intervals are 30-60s) — an honest, partial
    answer to "TLS already established": it helps once real polling
    activity is already flowing through this same client, not a
    standalone keep-alive ping run ahead of an otherwise-idle drop
    (not built this session — a still-cold first call after a long idle
    stretch pays the full handshake cost, which this project's own
    100ms target explicitly scopes to the WARM case only)."""
    return httpx.Client(
        timeout=timeout, limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=120.0)
    )


def build_default_purchase_registry() -> PurchaseConnectorRegistry:
    registry = PurchaseConnectorRegistry()
    agent_profile_url = get_ucp_agent_profile_url()

    for merchant in MERCHANTS:
        if merchant.name == "Fuji Store":
            registry.register(
                merchant.name,
                FujiStorePurchaseConnector(client=_persistent_client(_FUJI_TIMEOUT_SECONDS)),
            )
        elif merchant.name == "Cultura":
            registry.register(
                merchant.name,
                CulturaPurchaseConnector(client=_persistent_client(_CULTURA_TIMEOUT_SECONDS)),
            )
        elif merchant.name in _UCP_SHOP_DOMAINS and agent_profile_url:
            registry.register(
                merchant.name,
                ShopifyUCPPurchaseConnector(
                    shop_domain=_UCP_SHOP_DOMAINS[merchant.name],
                    agent_profile_url=agent_profile_url,
                    client=_persistent_client(_UCP_TIMEOUT_SECONDS),
                ),
            )
        else:
            registry.register(
                merchant.name, UnsupportedPurchaseConnector(merchant_name=merchant.name)
            )
    return registry
