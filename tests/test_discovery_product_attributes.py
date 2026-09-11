from __future__ import annotations

from discovery.product_attributes import extract_attributes


def test_set_code_detected_from_brackets() -> None:
    assert extract_attributes("[SCELLE] Tripack ME04 - Chaos Ascendant [FR]").set_code == "me04"


def test_set_code_with_decimal_point_kept_intact() -> None:
    """ "ME2.5" must not be split into "me2" and a separate "5" — the
    decimal point is what distinguishes it from "ME25"."""
    attrs = extract_attributes("[SCELLE] Tripack ME2.5 - Héros Transcendant [FR]")
    assert attrs.set_code == "me2.5"


def test_tripack_and_tri_pack_normalize_to_the_same_type() -> None:
    a = extract_attributes("[SCELLE] Tripack ME04 - Chaos Ascendant [FR]")
    b = extract_attributes("Tri-Pack Pokémon - Méga-Évolution - Chaos Ascendant [ME04] - FR")
    assert a.product_type == b.product_type == "tripack"


def test_etb_and_elite_trainer_box_normalize_to_the_same_type() -> None:
    a = extract_attributes("ETB Chaos Ascendant FR")
    b = extract_attributes("Elite Trainer Box Chaos Ascendant FR")
    assert a.product_type == b.product_type == "etb"


def test_coffret_dresseur_delite_is_recognized_as_etb() -> None:
    """The real French retail name for an Elite Trainer Box."""
    attrs = extract_attributes("Coffret Pokémon Dresseur d'Elite - Chaos Ascendant [ME04] - FR")
    assert attrs.product_type == "etb"


def test_display_and_booster_box_normalize_to_the_same_type() -> None:
    a = extract_attributes("Display ME04 Chaos Ascendant FR")
    b = extract_attributes("Booster Box ME04 Chaos Ascendant FR")
    assert a.product_type == b.product_type == "display"


def test_booster_sous_blister_is_blister_not_generic_booster() -> None:
    attrs = extract_attributes("[SCELLE] Booster sous Blister ME04 - Chaos Ascendant [FR]")
    assert attrs.product_type == "blister"


def test_plain_booster_is_its_own_type_distinct_from_display() -> None:
    booster = extract_attributes("Booster ME04 Chaos Ascendant FR")
    display = extract_attributes("Display ME04 Chaos Ascendant FR")
    assert booster.product_type == "booster"
    assert booster.product_type != display.product_type


def test_numbered_single_card_detected_as_single_card_type() -> None:
    attrs = extract_attributes("035/086 Méga-Floette ex")
    assert attrs.product_type == "single_card"


def test_language_tag_detected() -> None:
    assert extract_attributes("Tripack ME04 Chaos Ascendant FR").language == "fr"
    assert extract_attributes("Tripack ME04 Chaos Ascendant JP").language == "jp"


def test_french_word_de_is_not_mistaken_for_german_language_tag() -> None:
    """Regression: "SET DE BASE" (real Fuji Store product name) must not
    be detected as German just because "de" collides with a language tag."""
    attrs = extract_attributes("ALAKAZAM HOLO 1/102 - SET DE BASE - POKEMON FR 1999")
    assert attrs.language == "fr"


def test_no_set_code_or_type_or_language_is_none_not_guessed() -> None:
    attrs = extract_attributes("Some Random Item")
    assert attrs.set_code is None
    assert attrs.product_type is None
    assert attrs.language is None


def test_accented_characters_fold_into_plain_ascii_tokens() -> None:
    attrs = extract_attributes("Pokémon Méga-Évolution")
    assert "pokemon" in attrs.tokens
    assert "mega" in attrs.tokens
    assert "evolution" in attrs.tokens
