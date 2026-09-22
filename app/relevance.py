"""Is a catalogue hit worth an alert at all?

Why this exists — observed live on 21/09, from the first real run of the
catalogue watch: a shop's own search engine is a recall tool, not a
relevance filter. Asked for "pokemon 30 ans", RelicTCG returned a
Japanese Ancient Roar booster; Cultura returned a backpack and a Switch
game; Hikaru returned a dozen promo single cards. Every one of those
became a candidate alert. The watch trusted the shop's ranking blindly.

Three checks, cheapest first, all deterministic (no LLM, no network):

  1. exclusion terms — merchandise and accessories with no resale
     market for a sealed-product hunter (bags, plush, binders, sleeves,
     video games, graded/promo singles, ...), plus any per-watch extras;
  2. every significant term of the watch's keyword must really be in the
     product name — accent-folded, "30e"/"30ème" read as "30", and
     "ans"/"anniversaire" treated as the same word, because French shops
     name the same product both ways;
  3. in sealed-only mode (the default, for trading cards), the product
     must be a sealed product type: ETB, display, booster, bundle,
     coffret, tin, blister... — never a single card, never an unknown
     object. Product-type detection is shared with discovery matching
     (discovery/product_attributes.py), so the two never disagree about
     what a "coffret" or a "carte unitaire" is.

Sealed-only is a per-watch switch, not a global rule: a clothing or
sneaker watch turns it off and keeps only checks 1 (its own exclusions)
and 2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from discovery.product_attributes import extract_attributes, normalize

# Product types from discovery/product_attributes.py with a real resale
# market for sealed-product hunting. Deliberately absent: "deck" (theme
# and battle decks resell at or below retail), "single_card".
SEALED_TYPES = frozenset(
    {
        "etb",
        "display",
        "demi_display",
        "booster",
        "booster_bundle",
        "bundle",
        "coffret",
        "tin",
        "blister",
        "tripack",
        "duopack",
        "full_set",
    }
)

# Sealed products whose French names carry none of the generic type
# words above. Kept local rather than added to the shared discovery
# vocabulary, so discovery auto-link matching is not affected.
_EXTRA_SEALED_MARKERS = (
    "pokebox",
    "ultra premium",
    "upc",
    "mini tin",
    "coffre aux tresors",
    "kit avant premiere",
    "elite trainer",
    "collection premium",
    "collection illustration",
)

# Applied in sealed-only mode. Whole-word/phrase matches on the
# accent-folded name, so "sac" never hits "sachet".
DEFAULT_EXCLUDE_TERMS = (
    "sac",
    "sac a dos",
    "trousse",
    "peluche",
    "figurine",
    "figurines",
    "agenda",
    "classeur",
    "portfolio",
    "protege cartes",
    "sleeves",
    "pochettes",
    "jeu video",
    "switch",
    "nintendo",
    "t shirt",
    "sweat",
    "casquette",
    "mug",
    "puzzle",
    "livre",
    "manga",
    "lampe",
    "enceinte",
    "costume",
    "deguisement",
    "carte unitaire",
    "mep",
    "gradee",
    "grades",
    "psa",
    "pca",
)

_STOPWORDS = frozenset({"de", "du", "des", "la", "le", "les", "et", "d", "l", "the", "of", "a"})
_ORDINAL_RE = re.compile(r"^(\d+)(?:e|eme|er|th|nd|rd|st)$")
_SYNONYMS = {"ans": "anniversaire", "anniversary": "anniversaire", "anniv": "anniversaire"}


@dataclass(frozen=True, slots=True)
class RelevanceVerdict:
    relevant: bool
    reason: str


def _terms(text: str) -> set[str]:
    out: set[str] = set()
    for token in normalize(text).split():
        ordinal = _ORDINAL_RE.match(token)
        if ordinal:
            token = ordinal.group(1)
        out.add(_SYNONYMS.get(token, token))
    return out


def _contains_phrase(padded_name: str, phrase: str) -> bool:
    folded = normalize(phrase)
    return bool(folded) and f" {folded} " in padded_name


def parse_exclude_terms(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(term.strip() for term in raw.split(",") if term.strip())


def assess(
    name: str,
    *,
    keyword: str,
    sealed_only: bool = True,
    extra_exclude_terms: tuple[str, ...] = (),
) -> RelevanceVerdict:
    padded = f" {normalize(name)} "

    exclusions = (
        (*DEFAULT_EXCLUDE_TERMS, *extra_exclude_terms) if sealed_only else extra_exclude_terms
    )
    for term in exclusions:
        if _contains_phrase(padded, term):
            return RelevanceVerdict(False, f"excluded term {term!r}")

    name_terms = _terms(name)
    missing = sorted(t for t in _terms(keyword) - _STOPWORDS if t not in name_terms)
    if missing:
        return RelevanceVerdict(False, f"keyword term(s) not in name: {', '.join(missing)}")

    if not sealed_only:
        return RelevanceVerdict(True, "keyword match")

    product_type = extract_attributes(name).product_type
    if product_type == "single_card":
        return RelevanceVerdict(False, "single card")
    if product_type in SEALED_TYPES:
        return RelevanceVerdict(True, f"sealed product ({product_type})")
    if any(_contains_phrase(padded, marker) for marker in _EXTRA_SEALED_MARKERS):
        return RelevanceVerdict(True, "sealed product")
    return RelevanceVerdict(False, "not a recognised sealed product")
