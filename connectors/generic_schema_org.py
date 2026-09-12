"""Read-only connector for a retailer whose product page embeds a
schema.org Product JSON-LD block, but whose URL path has no single fixed
prefix the way Shopify (`/products/`) or WooCommerce (`/produit/`) do —
each product's own category segment(s) vary (e.g. Cultura's
`/p-<slug>.html`, E.Leclerc's `/fp/<slug>`, JouéClub's `/<category>/
<slug>.html`, La Grande Récré's `/<category>/<subcategory>/<slug>.html`).

external_id convention here is therefore the *entire* URL path (no
leading slash), not just the trailing segment — see scripts/watch.py's
_detect_from_url(), which special-cases MerchantDefinition.
full_path_external_id=True for exactly this reason.

Phase 28 audit: only registered for a retailer after confirming, by a
real plain HTTP GET (no browser, no anti-bot bypass), that its product
pages are reachable and do carry this JSON-LD block — see
connectors/defaults.py for which retailers passed that check and which
didn't (Fnac/King Jouet/Smyths/Carrefour/Micromania: real, confirmed
bot-protection blocks — Akamai/DataDome/Incapsula/Cloudflare — never
attempted to bypass).
"""

from __future__ import annotations

from connectors.schema_org import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USER_AGENT,
    SchemaOrgProductConnector,
    normalize_domain,
)


class GenericSchemaOrgConnector(SchemaOrgProductConnector):
    def __init__(
        self,
        *,
        shop_domain: str,
        merchant_name: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        super().__init__(merchant_name=merchant_name, timeout=timeout, user_agent=user_agent)
        self._shop_domain = normalize_domain(shop_domain)

    def _build_url(self, path_id: str) -> str:
        return f"https://{self._shop_domain}/{path_id.lstrip('/')}"
