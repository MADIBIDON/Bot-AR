"""Manual real-world connector check — makes one real HTTP GET. Not run by
pytest. Prints only normalized fields, never any secret.

Auto-detects the merchant/connector from the URL's hostname (see
connectors/defaults.py). For an unsupported hostname, falls back to a
plain ShopifyConnector guess (many small shops are Shopify) so you can
still probe an unlisted domain.

Usage:
    python scripts/test_real_connector.py "https://kairyu.fr/products/<handle>"
"""

from __future__ import annotations

import sys
from urllib.parse import urlparse

from connectors.base import ConnectorError, ProductNotFoundError
from connectors.defaults import find_merchant_for_domain, supported_domains_summary
from connectors.shopify import ShopifyConnector


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/test_real_connector.py <product_url>")
        return 1

    url = sys.argv[1]
    parsed = urlparse(url)
    if not parsed.netloc or not parsed.path:
        print(f"Could not parse a shop domain and product handle from: {url}")
        return 1

    handle = parsed.path.rstrip("/").rsplit("/", 1)[-1]

    merchant_def = find_merchant_for_domain(parsed.netloc)
    if merchant_def is not None:
        connector = merchant_def.build_connector()
        merchant_name = merchant_def.name
        connector_name = type(connector).__name__
    else:
        print(f"Unsupported domain {parsed.netloc!r} — known merchants:")
        print(supported_domains_summary())
        print("Falling back to a plain ShopifyConnector guess...")
        connector = ShopifyConnector(shop_domain=parsed.netloc, merchant_name=parsed.netloc)
        merchant_name = parsed.netloc
        connector_name = type(connector).__name__

    try:
        product = connector.get_product(handle)
    except (ConnectorError, ProductNotFoundError) as exc:
        print(f"Connector error: {exc}")
        return 1

    print(f"merchant:     {merchant_name}")
    print(f"connector:    {connector_name}")
    print(f"external_id:  {product.external_id}")
    print(f"name:         {product.name}")
    print(f"url:          {product.url}")
    print(f"price:        {product.price} {product.currency}")
    print(f"in_stock:     {product.available}")
    print(f"ean/gtin:     {product.ean}")
    print(f"mpn:          {product.mpn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
