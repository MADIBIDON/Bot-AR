"""Pokémon 30th-Anniversary ("30e Anniversaire", real set code ME5.5)
family classification — Phase 31 section 9.

Every family/keyword below was observed live this session on Fuji Store's
own real catalog search for the ME5.5 line (ETB, Pokébox Nymphali/
Amphinobi, Coffret Nymphali/Amphinobi, Coffret Collection Poster, Duopack
Evoli, Tripack Lucario, Tripack Noadkoko d'Alola, Pack Collection K.O
Evoli) — never invented. Reuses discovery/product_attributes.py's
product_type detector wherever it already recognizes the shape (etb,
coffret, duopack, tripack, display); adds only the handful of ME5.5-
specific markers that a general-purpose Pokémon TCG detector has no
reason to know about (pokebox, "collection poster") since those are this
one anniversary line's own retailer-facing naming, not a standard TCG
product type.

Pure, no I/O — classifies a name string only. A Session-backed campaign
status snapshot (grouping actual watched Products by family) belongs in
app/, same layering as app/opportunity_snapshot.py.
"""

from __future__ import annotations

from enum import StrEnum

from discovery.product_attributes import extract_attributes

# Both real spellings observed live in this project's own database:
# "30e Anniversaire" (Fuji Store's ME5.5 catalog, Phase 30) and "30eme
# Anniversaire" (this project's own earlier Phase 28 test-product names).
_ANNIVERSARY_MARKERS = ("30e anniversaire", "30eme anniversaire", "30th anniversary")


class Pokemon30eFamily(StrEnum):
    ETB = "etb"
    COFFRET_EX = "coffret_ex"
    COFFRET_POSTER = "coffret_poster"
    POKEBOX = "pokebox"
    BUNDLE = "bundle"  # duopack / tripack / pack collection
    DISPLAY = "display"
    OTHER = "other"


def is_30e_anniversary_product(name: str) -> bool:
    """True only for a real, explicit anniversary marker in the name —
    never inferred from the ME5.5 set code alone (a set code could in
    principle be reused elsewhere; every real ME5.5 product observed this
    session carried the explicit "30e Anniversaire"/"30th Anniversary"
    text, so that is the actual, honest signal to key off)."""
    folded = name.lower()
    return any(marker in folded for marker in _ANNIVERSARY_MARKERS)


def classify_30e_family(name: str) -> Pokemon30eFamily:
    folded = name.lower()
    if "collection poster" in folded or "poster" in folded:
        return Pokemon30eFamily.COFFRET_POSTER
    if "pokebox" in folded or "poke box" in folded or "poké box" in folded:
        return Pokemon30eFamily.POKEBOX

    attrs = extract_attributes(name)
    if attrs.product_type == "etb":
        return Pokemon30eFamily.ETB
    if attrs.product_type in ("duopack", "tripack", "bundle"):
        return Pokemon30eFamily.BUNDLE
    if attrs.product_type in ("display", "demi_display", "booster_bundle"):
        return Pokemon30eFamily.DISPLAY
    if attrs.product_type == "coffret":
        return Pokemon30eFamily.COFFRET_EX
    return Pokemon30eFamily.OTHER
