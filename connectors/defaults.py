"""Single place defining which merchants exist and how they're wired up.

Used by app/main_worker.py (to build the live ConnectorRegistry) and by
scripts/watch.py (to auto-detect a merchant/connector from a product
URL's hostname). Adding a merchant means adding one MerchantDefinition
here — nothing else in the pipeline changes.

Phase 28: capabilities are a plain, code-defined, versioned fact about
each merchant (what a real, honest check against their public site
confirmed today) rather than a DB table — nothing here is learned or
updated at runtime, so a table mirroring this constant would only add a
sync-drift risk with no behavioral benefit. CATALOG_SEARCH,
LOCAL_STORE_SEARCH, LOCAL_STOCK, and CLICK_AND_COLLECT are unsupported
for every Phase 28 retailer (Cultura/JouéClub/E.Leclerc/La Grande Récré):
each was confirmed reachable for a single product page fetch (ONLINE_STOCK
+ PRICE), but no public store-locator or per-store stock API was found
within this session's research budget — a real, documented gap, not a
guessed-and-skipped one. Fnac, King Jouet, Smyths Toys, Carrefour, and
Micromania were investigated and are UNSUPPORTED entirely: each returned
a confirmed bot-protection block (Akamai, DataDome, Imperva/Incapsula, or
Cloudflare) on a plain, honest HTTP GET to a real product page — never
attempted to bypass, per this project's standing rule.

Phase 30 (real-world canary coverage, 2026-09-13): Boulanger was audited
fresh and is genuinely reachable — a plain HTTP GET to a real product
page (`/ref/<id>`) returns real schema.org Product JSON-LD (price, gtin13,
availability), same shape GenericSchemaOrgConnector already handles for
Cultura/E.Leclerc — so it's onboarded the same way, ONLINE_STOCK+PRICE
only. Its own product sitemap (15 files x ~20k URLs, general electronics
merchandise) was NOT wired into discovery for the same "grosse charge"
reason as Cultura/E.Leclerc (see discovery/defaults.py). Its store pages
carry no structured LocalBusiness/geo data reachable within this
session's budget, so LOCAL_STORE_SEARCH/LOCAL_STOCK stay unimplemented —
a real, documented gap, not guessed-and-skipped. Fnac, King Jouet,
Smyths Toys, Carrefour, and Micromania were re-checked today and remain
blocked by the exact same vendors as Phase 28 (Akamai/DataDome/
Imperva/Cloudflare), confirmed again on both their product pages and
robots.txt/sitemap. Smyths' own robots.txt happens to be served from a
separate, un-blocked CDN (no product data there, just a sitemap index of
UK/DE/other-locale catalogs — no fr-fr product sitemap listed — so this
changes nothing about its UNSUPPORTED status). Amazon France was
investigated for a public, key-free surface (no PA-API credentials
requested/used) and stays ALERT_ONLY/UNSUPPORTED — see this module's
UNSUPPORTED_RETAILERS entry for the exact reasoning.

Phase 39 (Pokémon 30e drop readiness): King Jouet's PRODUCT PAGE remains
DataDome-blocked (re-confirmed again), but a distinct, genuinely public
JSON endpoint (`/api/product/<ref>`) was found and verified live against
3 real refs — real price/availability data on a plain GET, no special
headers. Their cart/basket API IS behind the same DataDome challenge as
the page (confirmed 403, same holding-page marker) and is never touched
— King Jouet moves from fully UNSUPPORTED to ONLINE_STOCK+PRICE
monitoring only, same tier as Boulanger/Cultura. Fnac and Carrefour were
re-checked the same way (product API guesses, robots.txt, sitemap) and
found no equivalent unprotected surface — they remain fully UNSUPPORTED.
See connectors/king_jouet.py for the connector and the exact identity
caveat (no EAN exposed by this API — matches on King Jouet's own
ref/sku instead, an exact-SKU tier per products/matcher.py, not a
downgrade to name-only matching).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from connectors.base import BaseConnector
from connectors.fake_store import FakeStoreConnector
from connectors.generic_schema_org import GenericSchemaOrgConnector
from connectors.king_jouet import KingJouetConnector
from connectors.nike_launch import NikeLaunchConnector
from connectors.registry import ConnectorRegistry
from connectors.shopify import ShopifyConnector
from connectors.woocommerce import WooCommerceConnector

CATALOG_SEARCH = "CATALOG_SEARCH"
ONLINE_STOCK = "ONLINE_STOCK"
PRICE = "PRICE"
SHIPPING = "SHIPPING"
LOCAL_STORE_SEARCH = "LOCAL_STORE_SEARCH"
LOCAL_STOCK = "LOCAL_STOCK"
CLICK_AND_COLLECT = "CLICK_AND_COLLECT"

_ONLINE_ONLY_CAPABILITIES = frozenset({ONLINE_STOCK, PRICE})
# Kairyu/RelicTCG/Fuji also have a real, working catalog search — see
# discovery/defaults.py (Shopify UCP search_catalog / WooCommerce Store
# API search) — unlike the four Phase 28 retailers below, which are
# product-page monitoring only (no confirmed public search endpoint).
_ONLINE_WITH_SEARCH_CAPABILITIES = frozenset({CATALOG_SEARCH, ONLINE_STOCK, PRICE})
# Phase 29: JouéClub and La Grande Récré run the same Proximis/Rbs
# platform — confirmed (real JS inspection, not guessed) to expose a
# sitemap-based catalog search (discovery/sitemap.py) AND a genuinely
# public store-locator + per-SKU store-stock API (discovery/store_locator.py
# / market stock check) — see connectors/defaults.py and
# local_stock/store_locator modules for exactly what was verified.
_FULL_RBS_PLATFORM_CAPABILITIES = frozenset(
    {CATALOG_SEARCH, ONLINE_STOCK, PRICE, LOCAL_STORE_SEARCH, LOCAL_STOCK, CLICK_AND_COLLECT}
)


@dataclass(frozen=True)
class MerchantDefinition:
    name: str
    domains: tuple[str, ...]
    build_connector: Callable[[], BaseConnector]
    # True only for a connector whose external_id must be the product
    # page's entire URL path (see connectors/generic_schema_org.py) —
    # Shopify/WooCommerce use a single fixed path prefix instead, so their
    # external_id is just the trailing handle/slug (the default, False).
    full_path_external_id: bool = False
    capabilities: frozenset[str] = field(default_factory=lambda: _ONLINE_ONLY_CAPABILITIES)


MERCHANTS: tuple[MerchantDefinition, ...] = (
    MerchantDefinition(
        name="Kairyu",
        domains=("kairyu.fr", "www.kairyu.fr"),
        build_connector=lambda: ShopifyConnector(shop_domain="kairyu.fr", merchant_name="Kairyu"),
        capabilities=_ONLINE_WITH_SEARCH_CAPABILITIES,
    ),
    MerchantDefinition(
        name="RelicTCG",
        domains=("relictcg.com", "www.relictcg.com"),
        build_connector=lambda: ShopifyConnector(
            shop_domain="www.relictcg.com", merchant_name="RelicTCG"
        ),
        capabilities=_ONLINE_WITH_SEARCH_CAPABILITIES,
    ),
    MerchantDefinition(
        name="Fuji Store",
        domains=("fuji-store.fr", "www.fuji-store.fr"),
        build_connector=lambda: WooCommerceConnector(
            shop_domain="fuji-store.fr", merchant_name="Fuji Store"
        ),
        capabilities=_ONLINE_WITH_SEARCH_CAPABILITIES,
    ),
    # --- Shops seen carrying real Pokémon 30e drops on the reference
    # monitors while this project was blind to them. Each platform was
    # confirmed live by a plain GET to the shop's own public catalogue
    # API before being wired up — never assumed from the URL shape. ---
    MerchantDefinition(
        name="Boîte à Jeux",
        domains=("boite-a-jeux.fr", "www.boite-a-jeux.fr"),
        build_connector=lambda: WooCommerceConnector(
            shop_domain="boite-a-jeux.fr", merchant_name="Boîte à Jeux"
        ),
        capabilities=_ONLINE_WITH_SEARCH_CAPABILITIES,
    ),
    MerchantDefinition(
        name="Pokuji",
        domains=("pokuji.fr", "www.pokuji.fr"),
        build_connector=lambda: WooCommerceConnector(
            shop_domain="pokuji.fr", merchant_name="Pokuji"
        ),
        capabilities=_ONLINE_WITH_SEARCH_CAPABILITIES,
    ),
    MerchantDefinition(
        name="Hikaru Distribution",
        domains=("hikarudistribution.com", "www.hikarudistribution.com"),
        build_connector=lambda: ShopifyConnector(
            shop_domain="hikarudistribution.com", merchant_name="Hikaru Distribution"
        ),
        capabilities=_ONLINE_WITH_SEARCH_CAPABILITIES,
    ),
    # --- Phase 28: new P1 retailers, ONLINE_STOCK/PRICE only (see module
    # docstring) — each confirmed today via a real plain HTTP GET to a
    # real product page returning genuine schema.org Product JSON-LD. ---
    MerchantDefinition(
        name="Cultura",
        domains=("cultura.com", "www.cultura.com"),
        build_connector=lambda: GenericSchemaOrgConnector(
            shop_domain="www.cultura.com", merchant_name="Cultura"
        ),
        full_path_external_id=True,
        capabilities=_ONLINE_ONLY_CAPABILITIES,
    ),
    MerchantDefinition(
        name="JouéClub",
        domains=("joueclub.fr", "www.joueclub.fr"),
        build_connector=lambda: GenericSchemaOrgConnector(
            shop_domain="www.joueclub.fr", merchant_name="JouéClub"
        ),
        full_path_external_id=True,
        capabilities=_FULL_RBS_PLATFORM_CAPABILITIES,
    ),
    MerchantDefinition(
        name="E.Leclerc",
        domains=("e.leclerc", "www.e.leclerc"),
        build_connector=lambda: GenericSchemaOrgConnector(
            shop_domain="www.e.leclerc", merchant_name="E.Leclerc"
        ),
        full_path_external_id=True,
        capabilities=_ONLINE_ONLY_CAPABILITIES,
    ),
    MerchantDefinition(
        name="La Grande Récré",
        domains=("lagranderecre.fr", "www.lagranderecre.fr"),
        build_connector=lambda: GenericSchemaOrgConnector(
            shop_domain="www.lagranderecre.fr", merchant_name="La Grande Récré"
        ),
        full_path_external_id=True,
        capabilities=_FULL_RBS_PLATFORM_CAPABILITIES,
    ),
    # --- Phase 30: Boulanger, audited fresh this session — see module
    # docstring. ---
    MerchantDefinition(
        name="Boulanger",
        domains=("boulanger.com", "www.boulanger.com"),
        build_connector=lambda: GenericSchemaOrgConnector(
            shop_domain="www.boulanger.com", merchant_name="Boulanger"
        ),
        full_path_external_id=True,
        capabilities=_ONLINE_ONLY_CAPABILITIES,
    ),
    # --- Phase 30: a single, hardcoded scheduled-release test case — see
    # connectors/nike_launch.py. Deliberately NOT domain-detectable
    # (domains=()): this connector answers for exactly one launch page,
    # never a generic Nike product URL, so scripts/watch.py's URL-based
    # auto-detection must never route an arbitrary nike.com link here. ---
    MerchantDefinition(
        name="Nike SNKRS",
        domains=(),
        build_connector=lambda: NikeLaunchConnector(
            launch_url=(
                "https://www.nike.com/fr/launch/t/nike-sb-air-force-1-yuto-light-bone-and-iron-grey"
            ),
            style_color="IO8439-100",
            product_name="Nike SB Air Force 1 x Yuto 'Light Bone and Iron Grey'",
        ),
        capabilities=_ONLINE_ONLY_CAPABILITIES,
    ),
    # --- Phase 39: King Jouet's PRODUCT PAGE is still DataDome-blocked
    # (confirmed again this session), but their distinct
    # /api/product/<ref> JSON endpoint is genuinely public — a plain,
    # honest GET returns 200 with real price/availability data, verified
    # live against 3 real refs; their cart/basket API (/api/cart,
    # /api/basket) IS behind the same DataDome challenge as the page and
    # is never touched — see connectors/king_jouet.py. external_id here
    # is King Jouet's own bare numeric ref, not a URL path, so this entry
    # is NOT full_path_external_id (no generic URL routing either — the
    # ref is supplied directly, same posture as Nike SNKRS above). ---
    MerchantDefinition(
        name="King Jouet",
        domains=("king-jouet.com", "www.king-jouet.com"),
        build_connector=lambda: KingJouetConnector(),
        capabilities=_ONLINE_ONLY_CAPABILITIES,
    ),
)

# Phase 28: investigated and confirmed UNSUPPORTED — real bot-protection
# blocks on a plain HTTP GET to a real product page, never bypassed. Kept
# here (not silently omitted) so `scripts/watch.py status` can report
# them honestly instead of just not mentioning them at all.
UNSUPPORTED_RETAILERS: tuple[tuple[str, str], ...] = (
    ("Fnac", "blocked by Akamai bot protection (403 on a plain GET)"),
    ("Smyths Toys", "blocked by Imperva/Incapsula bot protection"),
    ("Carrefour", "blocked by Cloudflare bot protection (challenge required)"),
    ("Micromania", "blocked by Imperva/Incapsula bot protection"),
    ("Amazon", "ALERT_ONLY by explicit instruction — not integrated as a merchant this session"),
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
