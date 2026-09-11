"""Discovery-time product matching — deliberately separate from
products/matcher.py (which matches an already-known Listing's own
observation against a Product; it needs EAN/MPN/external_id fields that
free-text search results may or may not carry) and from
market_data/product_filter.py's is_comparable() (built for filtering
resale comps, reused here as the language/product-type conflict check
since that exact problem — "ETB vs Display", "FR vs JP" — is identical).

The one real risk in this module: auto-linking the wrong product into
live monitoring/purchasing. Every check below is conservative on
purpose — NO_MATCH is always preferred over a guessed auto-link; a
plausible-but-unconfirmed name match becomes a CANDIDATE (visible, never
auto-linked) rather than being upgraded on weak evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from market_data.product_filter import is_comparable

if TYPE_CHECKING:
    from connectors.base import ConnectorProduct
    from database.models import Product

DiscoveryVerdict = Literal["auto_link", "candidate", "no_match"]

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalize(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


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
    2. MPN/SKU exact on both sides -> auto_link, 90. Different -> no_match.
    3. No identifiers on either/both sides: is_comparable() must not
       detect a language or product-type conflict (rejects ETB-vs-
       Display, FR-vs-JP, ME04-vs-ME05, ...); if it does, no_match.
    4. Otherwise, an exact normalized-name match -> auto_link, 75 (name +
       set + type + language all effectively agreeing is the "forte
       confiance" bar for a name-only auto-link).
    5. Anything else that passed step 3 -> candidate, 50: plausible, but
       not confident enough to auto-link.
    """
    if product.ean and candidate.ean:
        if product.ean == candidate.ean:
            return DiscoveryMatch("auto_link", 100, "EAN/GTIN exact match.")
        return DiscoveryMatch("no_match", 0, "EAN/GTIN present on both sides and differs.")

    if product.mpn and candidate.mpn:
        if product.mpn == candidate.mpn:
            return DiscoveryMatch("auto_link", 90, "MPN/SKU exact match.")
        return DiscoveryMatch("no_match", 0, "MPN/SKU present on both sides and differs.")

    if not is_comparable(product.name, candidate.name):
        return DiscoveryMatch(
            "no_match", 0, "Language or product-type mismatch detected between names."
        )

    if _normalize(product.name) == _normalize(candidate.name):
        return DiscoveryMatch(
            "auto_link", 75, "Normalized product name matches exactly, no identifier conflict."
        )

    return DiscoveryMatch(
        "candidate",
        50,
        "Name is plausibly comparable but not an exact match — needs manual review.",
    )
