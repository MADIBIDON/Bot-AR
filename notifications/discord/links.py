"""Actionable links for an alert embed.

Two clearly separated kinds, and the distinction matters:

  * ADD-TO-CART links point at a merchant we actually monitor, and are
    the one-click path from a Discord ping to a filled basket. Only ever
    built from a URL pattern this project has really confirmed for that
    merchant's platform — never guessed. An unknown merchant yields
    None and the embed simply omits the field: a wrong cart link on a
    drop is worse than no link.

  * RESALE REFERENCE links (StockX / Cardmarket / eBay) are price
    references only, to judge whether an item is worth buying. They are
    never monitored, never bought from, and never treated as a source of
    stock — see the explicit instruction that only purchase sites are
    watch targets.

Nothing here performs a request; these are plain URLs a human clicks.
"""

from __future__ import annotations

from urllib.parse import quote_plus, urlsplit

# Merchants whose real add-to-cart URL pattern this project has actually
# confirmed. WooCommerce accepts `?add-to-cart=<product_id>` on any page
# of the store, which is why the site root is used rather than a
# locale-specific cart path (/cart/ vs /panier/ differ per install).
#
# Deliberately absent, and why:
#   King Jouet — cart/basket endpoints sit behind the same bot
#     protection as their product page; this project never touches them.
#   Shopify stores — the cart URL needs a VARIANT id, and our Shopify
#     connectors key on the product handle, so a link built from what we
#     store would be wrong. Left out until a variant id is actually
#     carried through.
_WOOCOMMERCE_MERCHANTS = frozenset({"Fuji Store"})


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def add_to_cart_url(*, merchant: str, external_id: str, product_url: str) -> str | None:
    """The real one-click cart URL for this merchant, or None when this
    project has no confirmed pattern for it. Never guesses."""
    if merchant not in _WOOCOMMERCE_MERCHANTS:
        return None
    if not external_id.isdigit():
        # WooCommerce's add-to-cart takes a numeric product id; a slug
        # would silently add nothing to the basket.
        return None
    origin = _origin(product_url)
    if origin is None:
        return None
    return f"{origin}/?add-to-cart={external_id}"


def resale_reference_links(product_name: str) -> dict[str, str]:
    """Search URLs used to judge resale value. References only — never
    monitored and never bought from."""
    query = quote_plus(product_name.strip())
    if not query:
        return {}
    return {
        "StockX": f"https://stockx.com/search?s={query}",
        "Cardmarket": (
            "https://www.cardmarket.com/en/Pokemon/Products/Search"
            f"?category=-1&searchString={query}"
        ),
        "eBay": f"https://www.ebay.fr/sch/i.html?_nkw={query}",
    }


def format_links_field(
    *, merchant: str, external_id: str, product_url: str, product_name: str
) -> str:
    """One compact markdown line for the embed's Links field, matching
    the reference monitors' layout: the cart link first when we have a
    real one, then the product page, then resale references."""
    parts: list[str] = []
    cart = add_to_cart_url(merchant=merchant, external_id=external_id, product_url=product_url)
    if cart is not None:
        parts.append(f"[ATC]({cart})")
    parts.append(f"[Link]({product_url})")
    parts.extend(f"[{label}]({url})" for label, url in resale_reference_links(product_name).items())
    return " | ".join(parts)
