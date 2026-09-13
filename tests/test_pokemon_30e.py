"""Family classification against the real ME5.5 product names observed
live on Fuji Store's own catalog search this session (Phase 30/31) — not
invented names."""

from __future__ import annotations

from products.pokemon_30e import (
    Pokemon30eFamily,
    classify_30e_family,
    is_30e_anniversary_product,
)


def test_etb_is_recognized() -> None:
    name = "ETB 30e ANNIVERSAIRE – ME5.5 – FRANCAIS 2026 POKEMON"
    assert is_30e_anniversary_product(name)
    assert classify_30e_family(name) == Pokemon30eFamily.ETB


def test_pokebox_is_recognized() -> None:
    name = "POKEBOX NYMPHALI EX 30e ANNIVERSAIRE – ME5.5 – FRANCAIS 2026 POKEMON"
    assert classify_30e_family(name) == Pokemon30eFamily.POKEBOX


def test_coffret_ex_is_recognized() -> None:
    name = "COFFRET AMPHINOBI EX 30e ANNIVERSAIRE – ME5.5 – FRANCAIS 2026 POKEMON"
    assert classify_30e_family(name) == Pokemon30eFamily.COFFRET_EX


def test_coffret_collection_poster_is_recognized() -> None:
    name = "COFFRET COLLECTION POSTER 30e ANNIVERSAIRE – ME5.5 – FRANCAIS 2026 POKEMON"
    assert classify_30e_family(name) == Pokemon30eFamily.COFFRET_POSTER


def test_duopack_is_a_bundle() -> None:
    name = "DUOPACK EVOLI 30e ANNIVERSAIRE – ME5.5 – FRANCAIS 2026 POKEMON"
    assert classify_30e_family(name) == Pokemon30eFamily.BUNDLE


def test_tripack_is_a_bundle() -> None:
    name = "TRIPACK LUCARIO 30e ANNIVERSAIRE – ME5.5 – FRANCAIS 2026 POKEMON"
    assert classify_30e_family(name) == Pokemon30eFamily.BUNDLE


def test_pack_collection_falls_back_to_other_never_misclassified_as_etb() -> None:
    """ "Pack Collection K.O Evoli" has no etb/coffret/duopack/tripack
    marker at all — must land in OTHER, never silently misfiled as a
    different real family (matcher-safety spirit applied to reporting
    too: an unrecognized shape must stay visibly unrecognized)."""
    name = "PACK COLLECTION K.O EVOLI 30e ANNIVERSAIRE – ME5.5 – FRANCAIS 2026 POKEMON"
    assert classify_30e_family(name) == Pokemon30eFamily.OTHER


def test_30eme_spelling_variant_is_recognized() -> None:
    """This project's own Phase 28 test products (created before Fuji
    Store's own ME5.5 catalog naming was known) used "30eme Anniversaire",
    not "30e Anniversaire" — both are real, both must be recognized."""
    assert is_30e_anniversary_product("Pokemon Coffret Dresseur d'Elite 30eme Anniversaire")
    assert (
        classify_30e_family("Pokemon Coffret Dresseur d'Elite 30eme Anniversaire")
        == Pokemon30eFamily.ETB
    )


def test_non_30e_product_is_not_flagged_as_anniversary() -> None:
    assert is_30e_anniversary_product("Pokémon EV11 : coffret Dresseur d'Elite") is False


def test_series_1_and_2_never_confused_with_series_3_by_this_classifier() -> None:
    """Regression guard: this module only classifies *family* (etb vs
    coffret vs bundle...), never set/series — it must never be mistaken
    for or substitute the discovery matcher's own set-code discrimination
    (see tests/test_discovery_matcher.py's Series 1/2/3 cases)."""
    name_s3 = "Premiers Partenaires Série 3 30e Anniversaire – ME5.5"
    assert classify_30e_family(name_s3) in (Pokemon30eFamily.OTHER, Pokemon30eFamily.COFFRET_EX)
