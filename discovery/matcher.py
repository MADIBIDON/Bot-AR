"""Discovery-time product matching — deliberately separate from
products/matcher.py (which matches an already-known Listing's own
observation against a Product; it needs EAN/MPN/external_id fields that
free-text search results may or may not carry) and from
market_data/product_filter.py's is_comparable() (built for filtering
resale comps against loose eBay listing titles — a different consumer,
its own tuned vocabulary/threshold, left untouched).

The one real risk in this module: auto-linking the wrong product into
live monitoring/purchasing. Every check below is conservative on
purpose — NO_MATCH is always preferred over a guessed auto-link; a
plausible-but-unconfirmed name match becomes a CANDIDATE (visible, never
auto-linked) rather than being upgraded on weak evidence.

Phase 23: beyond EAN/MPN, matching is now *structured* rather than a
single "exact normalized name or nothing" check — see
discovery/product_attributes.py for the extractor. A detected set code
(ME04), product type (tripack/etb/display/...), or language (fr/en/jp)
is a HARD constraint the instant both sides disagree — a mismatch there
is never a coincidence worth risking. When all three are confirmed
identical on both sides *and* the names still share enough real
descriptive overlap (e.g. "Chaos Ascendant"), that combination is strong
enough to auto-link even without an identical name — this is what lets
"[SCELLE] Tripack ME04 - Chaos Ascendant [FR]" (Kairyu) and "Tri-Pack
Pokémon - Méga-Évolution - Chaos Ascendant [ME04] - FR" (RelicTCG) link
as the same real product despite neither an identifier nor an identical
title, without opening the door to "ETB ME04 FR" vs "Display ME04 FR" or
"Tripack ME04 FR" vs "Tripack ME05 FR" ever being confused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from discovery.product_attributes import extract_attributes

if TYPE_CHECKING:
    from connectors.base import ConnectorProduct
    from database.models import Product

DiscoveryVerdict = Literal["auto_link", "candidate", "no_match"]

AUTO_LINK_CONFIDENCE_THRESHOLD = 80

MIN_TOKEN_OVERLAP_RATIO = 0.3


@dataclass(frozen=True, slots=True)
class DiscoveryMatch:
    verdict: DiscoveryVerdict
    confidence: int
    reason: str


def classify_candidate(product: Product, candidate: ConnectorProduct) -> DiscoveryMatch:
    """Confidence tiers, in order, first conclusive check wins:

    1. EAN/GTIN exact on both sides -> auto_link, 100. Present on both
       but different -> no_match (a contradicting identifier is never a
       coincidence).
    2. MPN/SKU exact on both sides -> auto_link, 95. Different -> no_match.
    3. Structured hard constraints: set code, language, or product type
       detected and DIFFERING on both sides -> no_match. Detected on only
       one side, or not detected on either, is never treated as a
       conflict — absence isn't evidence.
    4. No meaningful token overlap at all (<30% of the reference
       product's own words) -> no_match: names that share almost nothing
       are more likely an unrelated product than the same one.
    5. Normalized names identical -> auto_link, 95.
    6. Set code + product type + language all confirmed identical on both
       sides, *and* the names still share at least 2 other real words
       (e.g. the set's own name, "Chaos Ascendant") -> auto_link, 88: the
       "forte confiance" bar for a non-identical name. Only 1 shared word
       -> candidate, 65. Zero -> candidate, 55 (structure agrees, but
       nothing to actually confirm it's the same listing's exact name).
    7. Anything else that passed step 4 -> candidate, 50: plausible, but
       not confident enough to auto-link.
    """
    if product.ean and candidate.ean:
        if product.ean == candidate.ean:
            return DiscoveryMatch("auto_link", 100, "EAN/GTIN exact match.")
        return DiscoveryMatch("no_match", 0, "EAN/GTIN present on both sides and differs.")

    if product.mpn and candidate.mpn:
        if product.mpn == candidate.mpn:
            return DiscoveryMatch("auto_link", 95, "MPN/SKU exact match.")
        return DiscoveryMatch("no_match", 0, "MPN/SKU present on both sides and differs.")

    ref = extract_attributes(product.name)
    cand = extract_attributes(candidate.name)

    if ref.set_code and cand.set_code and ref.set_code != cand.set_code:
        return DiscoveryMatch(
            "no_match", 0, f"Set code mismatch ({ref.set_code} vs {cand.set_code})."
        )
    if ref.language and cand.language and ref.language != cand.language:
        return DiscoveryMatch(
            "no_match", 0, f"Language mismatch ({ref.language} vs {cand.language})."
        )
    if ref.product_type and cand.product_type and ref.product_type != cand.product_type:
        return DiscoveryMatch(
            "no_match", 0, f"Product type mismatch ({ref.product_type} vs {cand.product_type})."
        )

    if not ref.tokens:
        return DiscoveryMatch("candidate", 50, "Product has no comparable name to match against.")

    overlap_ratio = len(ref.tokens & cand.tokens) / len(ref.tokens)
    if overlap_ratio < MIN_TOKEN_OVERLAP_RATIO:
        return DiscoveryMatch(
            "no_match", 0, "Names share no meaningful overlap — likely a different product."
        )

    if ref.tokens == cand.tokens:
        return DiscoveryMatch(
            "auto_link",
            95,
            "Normalized product name matches exactly, no identifier or structural conflict.",
        )

    structural_triple_confirmed = (
        ref.set_code is not None
        and ref.set_code == cand.set_code
        and ref.product_type is not None
        and ref.product_type == cand.product_type
        and ref.language is not None
        and ref.language == cand.language
    )
    if structural_triple_confirmed:
        shared_residual = (ref.tokens & cand.tokens) - {ref.set_code, ref.language}
        if len(shared_residual) >= 2:
            return DiscoveryMatch(
                "auto_link",
                88,
                "Set code, product type and language all match; shared name confirms same product.",
            )
        if len(shared_residual) >= 1:
            return DiscoveryMatch(
                "candidate",
                65,
                "Set code, product type and language match but name overlap is weak — "
                "needs manual review.",
            )
        return DiscoveryMatch(
            "candidate",
            55,
            "Set code, product type and language match but no shared descriptive words — "
            "needs manual review.",
        )

    return DiscoveryMatch(
        "candidate",
        50,
        "Name is plausibly comparable but not an exact match — needs manual review.",
    )
