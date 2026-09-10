from __future__ import annotations

from market_data.product_filter import is_comparable


def test_identical_names_are_comparable() -> None:
    assert is_comparable("Pokemon ETB Ecarlate Violet FR", "Pokemon ETB Ecarlate Violet FR")


def test_different_language_is_rejected() -> None:
    assert not is_comparable("Pokemon ETB Ecarlate Violet FR", "Pokemon ETB Scarlet Violet EN")


def test_different_product_type_is_rejected() -> None:
    assert not is_comparable("Pokemon Display Ecarlate Violet FR", "Pokemon ETB Ecarlate Violet FR")


def test_candidate_missing_language_signal_is_not_rejected() -> None:
    # Reference has a language tag, candidate title just doesn't mention one —
    # that's not enough information to reject on absence alone.
    assert is_comparable(
        "Pokemon ETB Ecarlate Violet FR", "Pokemon Elite Trainer Box Ecarlate Violet"
    )


def test_low_word_overlap_is_rejected() -> None:
    assert not is_comparable("Pokemon ETB Ecarlate Violet FR", "Yu-Gi-Oh Structure Deck Random")


def test_empty_reference_name_is_always_comparable() -> None:
    assert is_comparable("", "anything at all")
