"""Read-only connector for any Shopify storefront.

Product page URL: `https://<shop_domain>/products/<handle>`. Parsing
mechanics (schema.org Product JSON-LD) are shared with other platforms —
see connectors/schema_org.py. Only the URL shape is Shopify-specific.

First real merchants wired up with this: Kairyu (kairyu.fr) and RelicTCG
(relictcg.com), both French TCG shops; the mechanism is generic to any
Shopify store, only `shop_domain` changes.
"""

from __future__ import annotations

from connectors.schema_org import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USER_AGENT,
    SchemaOrgProductConnector,
    normalize_domain,
)


class ShopifyConnector(SchemaOrgProductConnector):
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
        return f"https://{self._shop_domain}/products/{path_id}"
