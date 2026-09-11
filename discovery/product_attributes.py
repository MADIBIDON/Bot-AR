"""Structured attribute extraction for discovery-time matching (Phase 23).

Deliberately separate from market_data/product_filter.py's is_comparable()
— both answer "does this title look like the same product", but that one
is tuned for loose eBay-listing-title resale comps (its own vocabulary and
a word-overlap floor) and is reused there unchanged. Discovery auto-links
straight into real monitoring/purchasing, so it needs a stricter,
purpose-built extractor: an explicit set code (ME04, EV09, ME2.5, ...), a
wider synonym-normalized product-type vocabulary (booster vs booster
bundle vs blister vs display vs tripack vs ...), and a single-card-number
signal (035/086) to keep "carte unitaire" and "produit scellé" from ever
being confused — none of which the eBay-comp filter needs or attempts.

Every detector here follows the same conservative rule as the rest of
this module and market_data/product_filter.py: a value is only ever
*compared* when it was actually detected on both sides. Absence of a
signal is never treated as a conflict — there just isn't enough
information to reject on that alone.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_WORD_RE = re.compile(r"[a-z0-9]+")
_SET_CODE_RE = re.compile(r"\b[a-z]{2,3}\d{1,2}(?:\.\d)?\b", re.IGNORECASE)
_CARD_NUMBER_RE = re.compile(r"\b\d{1,3}/\d{1,3}\b")

# No "de": on real French merchant catalogs it collides constantly with
# the ordinary French preposition/particle "de" ("SET DE BASE", "boite DE
# rangement", ...) — found live against Fuji Store's real Alakazam listing
# during Phase 23 testing, where it falsely detected German. Every real
# merchant this project watches is French-market, so German isn't a
# realistic language tag to need here anyway.
_LANGUAGE_TAGS = {"fr", "en", "jp", "jap", "us", "es", "it"}

# Ordered most-specific-first: a name is matched against each in turn and
# the first hit wins, so e.g. "booster sous blister" resolves to "blister"
# rather than falling through to the generic "booster" catch-all, and
# "coffret dresseur d'elite" (the real French name for an Elite Trainer
# Box) resolves to "etb" rather than the generic "coffret".
_PRODUCT_TYPE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("booster_bundle", ("booster bundle",)),
    ("blister", ("blister", "sous blister")),
    ("demi_display", ("demi display", "half display")),
    ("display", ("display", "booster box", "booster display")),
    (
        "etb",
        (
            "etb",
            "elite trainer box",
            "coffret dresseur d elite",
            "coffret pokemon dresseur d elite",
        ),
    ),
    ("full_set", ("full set",)),
    ("tripack", ("tripack", "tri pack", "3 pack")),
    ("duopack", ("duopack", "duo pack", "2 pack")),
    ("bundle", ("bundle",)),
    ("coffret", ("coffret", "collection box", "collector chest")),
    ("deck", ("deck",)),
    ("tin", ("tin",)),
    ("booster", ("booster",)),
    ("single_card", ("carte unitaire", "single card")),
)


@dataclass(frozen=True, slots=True)
class ProductAttributes:
    tokens: frozenset[str]
    set_code: str | None
    product_type: str | None
    language: str | None


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _fold(text: str) -> str:
    """Lowercase + accent-folded (Pokémon -> pokemon, Méga-Évolution ->
    mega-evolution), punctuation still intact. Accent folding matters
    here specifically because real French set/franchise names are full of
    them and a plain [a-z0-9] regex silently mangles accented letters
    instead of matching them."""
    return _strip_accents(text).lower()


def normalize(text: str) -> str:
    """_fold(), then alnum-only tokens joined by single spaces. Loses
    punctuation entirely — including the decimal point in a set code like
    "ME2.5" — which is fine for token/type/language comparison but wrong
    for set-code detection, so _detect_set_code runs on _fold() output
    directly, before this collapses "me2.5" into "me2" + "5"."""
    return " ".join(_WORD_RE.findall(_fold(text)))


def _detect_set_code(folded_name: str) -> str | None:
    match = _SET_CODE_RE.search(folded_name)
    return match.group(0) if match else None


def _detect_product_type(folded_name: str, normalized_name: str) -> str | None:
    for type_name, markers in _PRODUCT_TYPE_MARKERS:
        if any(marker in normalized_name for marker in markers):
            return type_name
    # Checked against folded_name, not normalized_name: the "/" in a card
    # number like "035/086" is punctuation, already gone by the time
    # _WORD_RE has split normalized_name into separate "035" "086" tokens.
    if _CARD_NUMBER_RE.search(folded_name):
        return "single_card"
    return None


def _detect_language(normalized_name: str) -> str | None:
    tokens = normalized_name.split()
    return next((token for token in tokens if token in _LANGUAGE_TAGS), None)


def extract_attributes(name: str) -> ProductAttributes:
    folded_name = _fold(name)
    normalized_name = " ".join(_WORD_RE.findall(folded_name))
    return ProductAttributes(
        tokens=frozenset(normalized_name.split()),
        set_code=_detect_set_code(folded_name),
        product_type=_detect_product_type(folded_name, normalized_name),
        language=_detect_language(normalized_name),
    )
