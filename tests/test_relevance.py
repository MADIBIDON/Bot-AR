"""app/relevance.py — every name below is real: either noise the
catalogue watch actually surfaced on 21/09, or a genuine Pokémon 30e
drop announced by the reference monitors on 16-18/09."""

from __future__ import annotations

import pytest

from app.relevance import assess, parse_exclude_terms

KW = "pokemon 30 ans"


@pytest.mark.parametrize(
    "name",
    [
        # Real reference-monitor drops that MUST get through.
        "Pokémon 30 ans - Coffret dresseur d'élite",
        "Pokemon 30 ans - Pack de 2 boosters",
        "Pokémon 30 ans - Coffret poster",
        "Pokémon 30 ans - Coffret Nymphali-ex",
        "Coffret 4 Boosters Amphinobi EX Pokémon 30e Anniversaire",
        "2 Boosters Evoli Pokémon 30e Anniversaire",
        "ETB (Coffret Dresseur d'Élite) — 30ème Anniversaire Pokémon - Français",
        "Coffret Pokémon 30 ans Pokémon day – FR",
        "Pack 2 Boosters - Pokemon - 30 Ans",
    ],
)
def test_real_drops_are_relevant(name: str) -> None:
    verdict = assess(name, keyword=KW)
    assert verdict.relevant, verdict.reason


@pytest.mark.parametrize(
    ("name", "why"),
    [
        # Real noise from the first live run, 21/09.
        ("Sac à dos La Plume Dorée 30 cm - Bleu - Pokémon Pikachu", "excluded"),
        ("Légendes Pokémon : Z-A", "not in name"),
        ("Booster Pokémon - Ancient Roar - sv4k - JPN", "not in name"),
        ("Gruikui MEP FR 050 - Édition 30ème Anniversaire", "excluded"),
        ("Trio Starters 2G Gradés - Édition 30ème Anniversaire", "excluded"),
        # Real monitored merchandise that kept alerting for days.
        ("POKEMON -PELUCHE 20 CM 30 ans", "excluded"),
        ("Pokémon Classeur Pikachu 30 ans", "excluded"),
        ("Pokémon Lampe LED Evoli 30 ans", "excluded"),
    ],
)
def test_real_noise_is_rejected(name: str, why: str) -> None:
    verdict = assess(name, keyword=KW)
    assert not verdict.relevant
    assert why in verdict.reason


def test_single_card_with_card_number_is_rejected() -> None:
    verdict = assess("Pikachu 30 ans Pokémon 025/165 holo", keyword=KW)
    assert not verdict.relevant
    assert verdict.reason == "single card"


def test_unknown_object_is_rejected_in_sealed_mode() -> None:
    verdict = assess("Pokémon 30 ans horloge murale", keyword=KW)
    assert not verdict.relevant


def test_ordinal_and_synonym_variants_all_match() -> None:
    for name in (
        "Coffret Pokémon 30e anniversaire",
        "Coffret Pokémon 30ème Anniversaire",
        "Coffret Pokémon 30 ans",
        "Coffret Pokémon 30th Anniversary",
    ):
        assert assess(name, keyword=KW).relevant, name


def test_extra_sealed_markers_cover_french_names_without_type_words() -> None:
    assert assess("Pokébox Pokémon 30 ans Pikachu", keyword=KW).relevant
    assert assess("Coffret Ultra Premium Pokémon 30 ans", keyword=KW).relevant


def test_sac_never_matches_inside_another_word() -> None:
    """Whole-word matching: "sachet" is not "sac"."""
    assert assess("Sachet 3 boosters Pokémon 30 ans", keyword=KW).relevant


def test_non_tcg_watch_turns_sealed_mode_off() -> None:
    """A clothing watch must not demand a trading-card product type."""
    verdict = assess(
        "Nike SB Air Force 1 x Yuto Light Bone", keyword="nike sb yuto", sealed_only=False
    )
    assert verdict.relevant


def test_non_tcg_watch_still_honours_its_own_exclusions() -> None:
    verdict = assess(
        "Nike SB Air Force 1 x Yuto - enfant",
        keyword="nike sb yuto",
        sealed_only=False,
        extra_exclude_terms=("enfant",),
    )
    assert not verdict.relevant


def test_per_watch_extra_exclusions_apply_in_sealed_mode_too() -> None:
    verdict = assess(
        "Display Pokémon 30 ans japonais",
        keyword=KW,
        extra_exclude_terms=parse_exclude_terms("japonais, jpn"),
    )
    assert not verdict.relevant


def test_parse_exclude_terms() -> None:
    assert parse_exclude_terms(" jpn, japonais ,, ") == ("jpn", "japonais")
    assert parse_exclude_terms(None) == ()
