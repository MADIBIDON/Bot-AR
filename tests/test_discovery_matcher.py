from __future__ import annotations

from decimal import Decimal

from connectors.base import ConnectorProduct
from database.models import Product
from discovery.matcher import AUTO_LINK_CONFIDENCE_THRESHOLD, classify_candidate


def _product(**overrides: object) -> Product:
    defaults: dict[str, object] = dict(
        id=1, name="ETB Pokemon Chaos Ascendant FR", ean=None, mpn=None
    )
    defaults.update(overrides)
    return Product(**defaults)  # type: ignore[arg-type]


def _candidate(**overrides: object) -> ConnectorProduct:
    defaults: dict[str, object] = dict(
        external_id="etb-chaos-ascendant-fr",
        name="ETB Pokemon Chaos Ascendant FR",
        price=Decimal("59.90"),
        currency="EUR",
        available=True,
        seller="Kairyu",
        url="https://kairyu.fr/products/etb-chaos-ascendant-fr",
        ean=None,
        mpn=None,
    )
    defaults.update(overrides)
    return ConnectorProduct(**defaults)  # type: ignore[arg-type]


def test_ean_exact_match_auto_links() -> None:
    product = _product(ean="1234567890123")
    candidate = _candidate(ean="1234567890123")

    match = classify_candidate(product, candidate)

    assert match.verdict == "auto_link"
    assert match.confidence == 100


def test_ean_mismatch_is_no_match_even_if_names_agree() -> None:
    product = _product(ean="1234567890123")
    candidate = _candidate(ean="9999999999999")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_mpn_exact_match_auto_links() -> None:
    product = _product(mpn="POKETBME04-FR")
    candidate = _candidate(mpn="POKETBME04-FR")

    match = classify_candidate(product, candidate)

    assert match.verdict == "auto_link"
    assert match.confidence == 95


def test_mpn_mismatch_is_no_match() -> None:
    product = _product(mpn="POKETBME04-FR")
    candidate = _candidate(mpn="POKETBME05-FR")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_exact_normalized_name_with_no_identifiers_auto_links() -> None:
    product = _product(name="ETB Pokemon Chaos Ascendant FR")
    candidate = _candidate(name="ETB Pokemon Chaos Ascendant FR!")  # punctuation-only diff

    match = classify_candidate(product, candidate)

    assert match.verdict == "auto_link"
    assert match.confidence == 95


def test_similar_but_not_exact_name_is_candidate_only() -> None:
    product = _product(name="ETB Pokemon Chaos Ascendant FR")
    candidate = _candidate(name="Elite Trainer Box Chaos Ascendant Pokemon France")

    match = classify_candidate(product, candidate)

    assert match.verdict == "candidate"
    assert match.confidence == 50


def test_language_mismatch_is_no_match() -> None:
    product = _product(name="ETB Pokemon Chaos Ascendant FR")
    candidate = _candidate(name="ETB Pokemon Chaos Ascendant JP")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_product_type_mismatch_is_no_match() -> None:
    product = _product(name="ETB Pokemon Chaos Ascendant FR")
    candidate = _candidate(name="Display Pokemon Chaos Ascendant FR")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_unrelated_product_is_no_match() -> None:
    product = _product(name="ETB Pokemon Chaos Ascendant FR")
    candidate = _candidate(name="Yu-Gi-Oh Structure Deck Random")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


# --- Phase 23: structured matching (real Product #5 case) -----------------


def test_tripack_vs_tri_pack_synonym_with_same_set_auto_links() -> None:
    """The real case that motivated Phase 23: same product, two merchants,
    two different but equivalent spellings of "Tripack", no shared
    identifier — must clear the auto-link bar on structure + name alone."""
    product = _product(name="[SCELLE] Tripack ME04 - Chaos Ascendant [FR]")
    candidate = _candidate(
        name="Tri-Pack Pokémon - Méga-Évolution - Chaos Ascendant [ME04] - FR",
        seller="RelicTCG",
    )

    match = classify_candidate(product, candidate)

    assert match.verdict == "auto_link"
    assert match.confidence >= AUTO_LINK_CONFIDENCE_THRESHOLD


def test_etb_vs_elite_trainer_box_synonym_with_same_set_auto_links() -> None:
    product = _product(name="[SCELLE] Elite Trainer Box ME04 - Chaos Ascendant [FR]")
    candidate = _candidate(
        name="Coffret Pokémon Dresseur d'Elite - Méga-Évolution - Chaos Ascendant [ME04] - FR",
        seller="RelicTCG",
    )

    match = classify_candidate(product, candidate)

    assert match.verdict == "auto_link"
    assert match.confidence >= AUTO_LINK_CONFIDENCE_THRESHOLD


def test_same_set_type_language_but_no_shared_words_is_weak_candidate() -> None:
    """Structure matches but there's barely any shared descriptive word —
    stays a (low-confidence) candidate, never an auto-link, since
    structure alone (set/type/language) isn't proof of the same set name."""
    product = _product(name="Tripack ME04 Chaos Ascendant FR")
    candidate = _candidate(name="Tripack ME04 Something Else Entirely FR")

    match = classify_candidate(product, candidate)

    assert match.verdict == "candidate"
    assert match.confidence < AUTO_LINK_CONFIDENCE_THRESHOLD


def test_wrong_set_code_is_hard_rejected() -> None:
    product = _product(name="Tripack ME04 Chaos Ascendant FR")
    candidate = _candidate(name="Tripack ME05 Chaos Ascendant FR")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"
    assert match.confidence == 0


def test_wrong_language_is_hard_rejected() -> None:
    product = _product(name="Tripack ME04 Chaos Ascendant FR")
    candidate = _candidate(name="Tripack ME04 Chaos Ascendant JP")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"
    assert match.confidence == 0


def test_wrong_product_type_is_hard_rejected() -> None:
    product = _product(name="Tripack ME04 Chaos Ascendant FR")
    candidate = _candidate(name="ETB ME04 Chaos Ascendant FR")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"
    assert match.confidence == 0


def test_ean_mismatch_hard_rejected_even_with_matching_structure() -> None:
    product = _product(name="Tripack ME04 Chaos Ascendant FR", ean="1111111111111")
    candidate = _candidate(name="Tri-Pack Chaos Ascendant ME04 FR", ean="2222222222222")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_mpn_mismatch_hard_rejected_even_with_matching_structure() -> None:
    product = _product(name="Tripack ME04 Chaos Ascendant FR", mpn="AAA-04")
    candidate = _candidate(name="Tri-Pack Chaos Ascendant ME04 FR", mpn="BBB-04")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_single_card_vs_sealed_product_is_hard_rejected() -> None:
    """ "carte unitaire ≠ produit scellé" — a numbered single card must
    never be confused with a sealed tripack/etb/display/... of the same
    set, even if both mention the same set code."""
    product = _product(name="[SCELLE] Tripack ME04 - Chaos Ascendant [FR]")
    candidate = _candidate(name="035/086 Some Card ME04 FR")

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_missing_set_code_on_one_side_is_not_a_conflict() -> None:
    """Absence of a signal is never treated as a conflict — only a
    detected disagreement is."""
    product = _product(name="Tripack Chaos Ascendant FR")  # no set code at all
    candidate = _candidate(name="Tripack ME04 Chaos Ascendant FR")

    match = classify_candidate(product, candidate)

    assert match.verdict != "no_match"


# --- Phase 28: the 30th Anniversary drop's own near-identical siblings --
# ("Nymphali-ex" / "Amphinobi-ex" ETBs share the exact same product type,
# language, and most descriptive words as the plain 30th Anniversary ETB
# and each other — the Pokemon name is the ONLY thing that differs. None
# of these carry a classic ME04-style set code, so the auto-link path
# that requires one never fires; the real protection here is exact-name
# non-identity (step 5) plus, whenever a real EAN is known, EAN mismatch
# (step 1) — tested both ways below.


def test_etb_30e_does_not_match_nymphali_ex_30e_by_name_alone() -> None:
    product = _product(name="Pokemon Coffret Dresseur d'Elite 30eme Anniversaire", ean=None)
    candidate = _candidate(name="Pokemon Coffret 30eme Anniversaire Nymphali-ex", ean=None)

    match = classify_candidate(product, candidate)

    assert match.verdict != "auto_link"


def test_etb_30e_does_not_match_amphinobi_ex_30e_by_name_alone() -> None:
    product = _product(name="Pokemon Coffret Dresseur d'Elite 30eme Anniversaire", ean=None)
    candidate = _candidate(name="Pokemon Coffret 30eme Anniversaire Amphinobi-ex", ean=None)

    match = classify_candidate(product, candidate)

    assert match.verdict != "auto_link"


def test_nymphali_ex_does_not_match_amphinobi_ex() -> None:
    product = _product(name="Pokemon Coffret 30eme Anniversaire Nymphali-ex", ean=None)
    candidate = _candidate(name="Pokemon Coffret 30eme Anniversaire Amphinobi-ex", ean=None)

    match = classify_candidate(product, candidate)

    assert match.verdict != "auto_link"


def test_nymphali_ex_hard_rejected_when_real_eans_disagree() -> None:
    product = _product(name="Pokemon Coffret 30eme Anniversaire Nymphali-ex", ean="1111111111111")
    candidate = _candidate(
        name="Pokemon Coffret 30eme Anniversaire Amphinobi-ex", ean="2222222222222"
    )

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


def test_etb_30e_hard_rejected_against_nymphali_ex_when_eans_disagree() -> None:
    product = _product(
        name="Pokemon Coffret Dresseur d'Elite 30eme Anniversaire", ean="3333333333333"
    )
    candidate = _candidate(
        name="Pokemon Coffret 30eme Anniversaire Nymphali-ex", ean="4444444444444"
    )

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"


# --- Phase 28: "Collection Illustration Premiers Partenaires" series ----
# Same collection name, only the series number differs — must never be
# confused with each other by name alone, and must hard-reject on a
# known EAN mismatch.


def test_series_3_does_not_match_series_2_by_name_alone() -> None:
    product = _product(
        name="Pokemon Collection Illustration Premiers Partenaires Serie 3", ean=None
    )
    candidate = _candidate(
        name="Pokemon Collection Illustration Premiers Partenaires Serie 2", ean=None
    )

    match = classify_candidate(product, candidate)

    assert match.verdict != "auto_link"


def test_series_3_does_not_match_series_1_by_name_alone() -> None:
    product = _product(
        name="Pokemon Collection Illustration Premiers Partenaires Serie 3", ean=None
    )
    candidate = _candidate(
        name="Pokemon Collection Illustration Premiers Partenaires Serie 1", ean=None
    )

    match = classify_candidate(product, candidate)

    assert match.verdict != "auto_link"


def test_series_3_hard_rejected_against_series_2_when_eans_disagree() -> None:
    product = _product(
        name="Pokemon Collection Illustration Premiers Partenaires Serie 3", ean="5555555555555"
    )
    candidate = _candidate(
        name="Pokemon Collection Illustration Premiers Partenaires Serie 2", ean="6666666666666"
    )

    match = classify_candidate(product, candidate)

    assert match.verdict == "no_match"
