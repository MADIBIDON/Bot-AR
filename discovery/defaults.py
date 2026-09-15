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

Phase 29: JouéClub and La Grande Récré have no public catalog-search API
(confirmed this session), but both publish a single, reasonably-sized
product sitemap (~18k-40k URLs) — see discovery/sitemap.py. Cultura and
E.Leclerc were investigated the same way and REJECTED for this: their
product sitemaps are 75-87 files of 50,000 URLs each (3.75-4.35 million
SKUs total, a general-merchandise catalog, not toy-specialist) — a full
crawl of that size on a periodic discovery cycle would be exactly the
"grosse charge" this project avoids, and there is no evident way to
scope it to a smaller, relevant slice without more research than this
session had time for. A real, documented gap, not a guessed-and-skipped
one — see connectors/defaults.py's module docstring.

Phase 30: Boulanger's product sitemap (15 files x ~20k URLs, general
electronics — not toy-specialist) was checked and rejected for the same
"grosse charge" reason as Cultura/E.Leclerc.

Phase 36 (Pokémon 30e, Cultura-only campaign): Cultura's sitemap crawl
stays rejected (unchanged), but a different, much lighter surface was
found and confirmed live — the same public, unauthenticated Magento 2
GraphQL endpoint (`/m2/graphql`) Cultura's own search page calls, one
targeted query per discovery run, never a crawl. See
discovery/cultura.py's module docstring for the full recon (real GET,
no auth, real product data, real /p-<url_key>.html URL pattern
confirmed against two distinct live listings).
"""

from __future__ import annotations

from config.settings import get_ucp_agent_profile_url
from connectors.defaults import MERCHANTS
from discovery.cultura import CulturaSearchDiscoverySource
from discovery.registry import DiscoveryRegistry
from discovery.shopify_ucp import ShopifyUCPDiscoverySource
from discovery.sitemap import SitemapDiscoverySource
from discovery.woocommerce import WooCommerceDiscoverySource

_UCP_SHOP_DOMAINS = {
    "Kairyu": "kairyu.fr",
    "RelicTCG": "www.relictcg.com",
}
_WOOCOMMERCE_SHOP_DOMAINS = {
    "Fuji Store": "fuji-store.fr",
}
_SITEMAP_DISCOVERY_URLS = {
    "JouéClub": "https://www.joueclub.fr/Assets/Rbs/Seo/100185/fr_FR/Rbs_Catalog_Product.1.xml",
    "La Grande Récré": (
        "https://www.lagranderecre.fr/Assets/Rbs/Seo/100052/fr_FR/Rbs_Catalog_Product.1.xml"
    ),
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
        elif merchant.name in _SITEMAP_DISCOVERY_URLS:
            registry.register(
                merchant.name,
                SitemapDiscoverySource(
                    sitemap_index_url=_SITEMAP_DISCOVERY_URLS[merchant.name],
                    connector=merchant.build_connector(),
                    merchant_name=merchant.name,
                ),
            )
        elif merchant.name == "Cultura":
            # Phase 36: a single, targeted search query against Cultura's
            # own public Magento GraphQL endpoint — not the 3.75-4.35M
            # SKU sitemap crawl already rejected above. See
            # discovery/cultura.py's module docstring.
            registry.register(merchant.name, CulturaSearchDiscoverySource())
        # Merchants with none of the above are simply not registered —
        # the orchestrator (app/discovery.py) reports
        # DISCOVERY_UNAVAILABLE for any unregistered merchant and
        # continues with the others.
    return registry
