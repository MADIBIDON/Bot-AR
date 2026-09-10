"""Single place defining which market-data sources exist. Only "ebay" for
this phase — the instructions are explicit about not standing up a second
one yet.

Unlike connectors/defaults.py's build_default_registry() (which never
needs credentials — FakeStore/Shopify/WooCommerce are all secret-free),
this one needs EBAY_APP_ID/EBAY_CERT_ID. If they're missing, this raises
clearly rather than silently registering a broken source — callers
(app/main_worker.py, scripts/watch.py) decide what to do with that.
"""

from __future__ import annotations

from market_data.ebay import EbayMarketDataSource, load_ebay_config
from market_data.registry import MarketDataRegistry

SUPPORTED_MARKET_SOURCES = ("ebay",)


def build_default_market_registry() -> MarketDataRegistry:
    """Raises MissingEbayConfigError if EBAY_APP_ID/EBAY_CERT_ID are unset."""
    registry = MarketDataRegistry()
    registry.register("ebay", EbayMarketDataSource(config=load_ebay_config()))
    return registry
