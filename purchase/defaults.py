"""Single place defining which merchants have a working PurchaseConnector.

Kairyu and RelicTCG are Shopify storefronts; Fuji Store is WooCommerce.
None exposes a checkout path this project can automate without either
(a) handling real card data ourselves, which is never acceptable here, or
(b) scripting the storefront's own hosted checkout UI, which both
platforms protect with bot detection this project never bypasses (see
purchase/base.py's HumanActionRequiredError). Shopify's Storefront API
can build a cart and hand back a checkout URL, but completing payment
still requires either a human at that hosted page or handling real
payment-token/PCI flows outside this project's scope — not "a clean,
reliable, anti-bot-free path" as required for this phase.

So: every merchant here is wired to UnsupportedPurchaseConnector. The
purchase engine still runs its full validation pipeline and would create
a PurchaseIntent, but any real attempt ends in
AUTOMATED_CHECKOUT_UNSUPPORTED with a Discord alert and the manual
product link — never a scripted checkout. Adding a real connector later
means registering it here for exactly the merchants where one has been
verified to work cleanly; nothing else in the pipeline would need to
change.
"""

from __future__ import annotations

from connectors.defaults import MERCHANTS
from purchase.merchants.unsupported import UnsupportedPurchaseConnector
from purchase.registry import PurchaseConnectorRegistry


def build_default_purchase_registry() -> PurchaseConnectorRegistry:
    registry = PurchaseConnectorRegistry()
    for merchant in MERCHANTS:
        registry.register(merchant.name, UnsupportedPurchaseConnector(merchant_name=merchant.name))
    return registry
