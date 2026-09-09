"""Manual real-world connector check — makes one real HTTP GET. Not run by
pytest. Prints only normalized fields, never any secret (this connector
needs none).

Usage:
    python scripts/test_real_connector.py "https://kairyu.fr/products/<handle>"
"""

from __future__ import annotations

import sys
from urllib.parse import urlparse

from connectors.base import ConnectorError, ProductNotFoundError
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
    connector = ShopifyConnector(shop_domain=parsed.netloc, merchant_name=parsed.netloc)

    try:
        product = connector.get_product(handle)
    except (ConnectorError, ProductNotFoundError) as exc:
        print(f"Connector error: {exc}")
        return 1

    print(f"merchant:     {product.seller}")
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
