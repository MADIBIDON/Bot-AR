"""notifications/discord/links.py — pure URL construction, no network."""

from __future__ import annotations

from notifications.discord.links import (
    add_to_cart_url,
    format_links_field,
    resale_reference_links,
)


def test_woocommerce_merchant_gets_a_real_add_to_cart_url() -> None:
    url = add_to_cart_url(
        merchant="Fuji Store",
        external_id="143721",
        product_url="https://fuji-store.fr/produit/un-coffret/",
    )
    assert url == "https://fuji-store.fr/?add-to-cart=143721"


def test_unknown_merchant_never_gets_a_guessed_cart_url() -> None:
    """A wrong cart link on a drop is worse than no link."""
    assert (
        add_to_cart_url(
            merchant="King Jouet",
            external_id="1034916",
            product_url="https://www.king-jouet.com/jeu-jouet/ref-1034916",
        )
        is None
    )


def test_non_numeric_external_id_yields_no_cart_url() -> None:
    """WooCommerce's add-to-cart takes a numeric product id — a slug
    would silently add nothing to the basket."""
    assert (
        add_to_cart_url(
            merchant="Fuji Store",
            external_id="un-coffret-pokemon",
            product_url="https://fuji-store.fr/produit/un-coffret/",
        )
        is None
    )


def test_malformed_product_url_yields_no_cart_url() -> None:
    assert add_to_cart_url(merchant="Fuji Store", external_id="1", product_url="not-a-url") is None


def test_resale_links_cover_the_three_references() -> None:
    links = resale_reference_links("Pokémon 30 ans - Coffret poster")
    assert set(links) == {"StockX", "Cardmarket", "eBay"}
    assert all(u.startswith("https://") for u in links.values())
    assert "Coffret+poster" in links["eBay"]


def test_resale_links_empty_for_blank_name() -> None:
    assert resale_reference_links("   ") == {}


def test_links_field_puts_cart_first_then_product_then_references() -> None:
    field = format_links_field(
        merchant="Fuji Store",
        external_id="143721",
        product_url="https://fuji-store.fr/produit/un-coffret/",
        product_name="Coffret Amphinobi",
    )
    assert field.startswith("[ATC](https://fuji-store.fr/?add-to-cart=143721)")
    assert "[Link](https://fuji-store.fr/produit/un-coffret/)" in field
    assert "[StockX]" in field and "[Cardmarket]" in field and "[eBay]" in field


def test_links_field_omits_cart_when_unknown_merchant() -> None:
    field = format_links_field(
        merchant="King Jouet",
        external_id="1034916",
        product_url="https://www.king-jouet.com/jeu-jouet/ref-1034916",
        product_name="Coffret dresseur d'élite",
    )
    assert "[ATC]" not in field
    assert field.startswith("[Link](")
