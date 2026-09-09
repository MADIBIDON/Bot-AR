"""Read-only connector for a WooCommerce storefront with an SEO plugin
(Yoast, RankMath, ...) that embeds schema.org Product JSON-LD.

Product page URL: `https://<shop_domain>/<product_path>/<slug>/`
(`product_path` defaults to "produit", matching common French WooCommerce
setups; override it for stores using the English "product" slug or a
custom permalink structure). Parsing mechanics are identical to
ShopifyConnector — see connectors/schema_org.py; only the URL shape
differs, which is why this is a real second connector rather than a
Shopify-specific hack.

First real merchant wired up with this: Fuji Store (fuji-store.fr), a
French Pokémon/TCG card shop.
"""

from __future__ import annotations

from connectors.schema_org import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USER_AGENT,
    SchemaOrgProductConnector,
    normalize_domain,
)


class WooCommerceConnector(SchemaOrgProductConnector):
    def __init__(
        self,
        *,
        shop_domain: str,
        merchant_name: str,
        product_path: str = "produit",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        super().__init__(merchant_name=merchant_name, timeout=timeout, user_agent=user_agent)
        self._shop_domain = normalize_domain(shop_domain)
        self._product_path = product_path.strip("/")

    def _build_url(self, path_id: str) -> str:
        return f"https://{self._shop_domain}/{self._product_path}/{path_id}/"
