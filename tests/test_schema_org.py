"""Regression test for a real bug found while onboarding RelicTCG: some
pages ship more than one Product JSON-LD block — a generic theme
placeholder (wrong price, wrong currency, no url/sku) alongside the real
per-product data. No network: reads a local fixture only.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from connectors.schema_org import _extract_product_ld_json

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_duplicate_blocks_prefers_one_matching_page_url() -> None:
    html = (FIXTURES_DIR / "schema_org_duplicate_product_blocks.html").read_text(encoding="utf-8")
    page_url = "https://example-shopify-shop.test/products/duplicate-blocks-test-fr"

    data = _extract_product_ld_json(html, page_url)

    assert data is not None
    assert data["url"] == page_url
    assert data["sku"] == "REALSKU-001"
    offer = data["offers"][0]
    assert offer["price"] == 210.0
    assert offer["priceCurrency"] == "EUR"


def test_duplicate_blocks_ignores_the_placeholder() -> None:
    html = (FIXTURES_DIR / "schema_org_duplicate_product_blocks.html").read_text(encoding="utf-8")
    page_url = "https://example-shopify-shop.test/products/duplicate-blocks-test-fr"

    data = _extract_product_ld_json(html, page_url)

    # The placeholder block has price="21000" priceCurrency="USD" — must
    # never be what gets returned.
    offer = data["offers"][0]
    assert Decimal(str(offer["price"])) != Decimal("21000")
    assert offer["priceCurrency"] != "USD"


def test_single_block_is_returned_as_is() -> None:
    offer = {"@type": "Offer", "price": "10.00", "priceCurrency": "EUR"}
    offer["availability"] = "https://schema.org/InStock"
    payload = {"@type": "Product", "name": "Solo", "offers": offer}
    html = f'<script type="application/ld+json">{json.dumps(payload)}</script>'

    data = _extract_product_ld_json(html, "https://example.test/products/solo")

    assert data is not None
    assert data["name"] == "Solo"
