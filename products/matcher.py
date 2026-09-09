"""Product matcher — decides whether a ProductObservation is our Product.

Deterministic, explainable, conservative: no ML, no network, no database
access (all objects passed in must already be loaded). Independent of
connectors, monitoring, and the decision/purchase engine.

Confidence scale (0-100), evaluated in this strict priority order with a
short-circuit as soon as a level is conclusive (either an exact match or an
explicit contradiction):

    1. EAN/GTIN exact         -> 100 (the only path to 100 — deterministic,
                                  GS1-assigned, intrinsic to the product)
       EAN/GTIN both present, different -> hard reject (0), even if the name
       looks similar.
    2. MPN exact               -> 90  (used only when EAN was absent/unusable
                                  on at least one side)
       MPN both present, different -> hard reject (0). Stricter than the
       "negative signal" wording in the brief: a contradicting manufacturer
       identifier is treated the same as a contradicting EAN, to stay
       conservative.
    3. expected_listing.external_id exact -> 80 (only when a Listing the
       caller expects to be looking at is provided; requires the observed
       merchant to match the listing's merchant first)
       external_id present on both sides, different -> hard reject (0).
       Wrong merchant for the expected listing -> hard reject (0).
    4. Normalized name exact   -> 50. Never enough alone for an automated
       purchase decision — capped well below any identifier-based tier.
    5. Approximate name match  -> <=29, matched=False. An indication only,
       never treated as a match.
    -  Nothing comparable      -> 0, matched=False, insufficient_data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database.models import Listing, Product
    from products.observation import ProductObservation

CONFIDENCE_EAN_EXACT = 100
CONFIDENCE_MPN_EXACT = 90
CONFIDENCE_EXTERNAL_ID_EXACT = 80
CONFIDENCE_NAME_EXACT = 50
CONFIDENCE_NAME_APPROXIMATE_MAX = 29
CONFIDENCE_NONE = 0

_NAME_SIMILARITY_THRESHOLD = 0.6

_NON_DIGIT_RE = re.compile(r"\D+")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Explains, not just states, whether an observation is our product."""

    matched: bool
    confidence: int
    reason: str
    method: str

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")


def match_product(
    product: Product,
    observation: ProductObservation,
    expected_listing: Listing | None = None,
) -> MatchResult:
    """Compare a Product to an observed ProductObservation.

    `expected_listing`, if given, lets external_id be used as a signal —
    scoped to that merchant, since Product intentionally carries no
    merchant-specific identifier.
    """
    ean_result = _match_by_ean(product, observation)
    if ean_result is not None:
        return ean_result

    mpn_result = _match_by_mpn(product, observation)
    if mpn_result is not None:
        return mpn_result

    if expected_listing is not None:
        listing_result = _match_by_listing(observation, expected_listing)
        if listing_result is not None:
            return listing_result

    return _match_by_name(product, observation)


def _match_by_ean(product: Product, observation: ProductObservation) -> MatchResult | None:
    product_ids = {
        normalized
        for normalized in (_normalize_gtin(product.ean), _normalize_gtin(product.gtin))
        if normalized is not None
    }
    obs_ean = _normalize_gtin(observation.ean)

    if obs_ean is None or not product_ids:
        return None

    if obs_ean in product_ids:
        return MatchResult(
            matched=True,
            confidence=CONFIDENCE_EAN_EXACT,
            method="ean_exact",
            reason="Product and observation share the same EAN/GTIN.",
        )
    return MatchResult(
        matched=False,
        confidence=CONFIDENCE_NONE,
        method="ean_mismatch",
        reason="Product and observation have a different EAN/GTIN.",
    )


def _match_by_mpn(product: Product, observation: ProductObservation) -> MatchResult | None:
    product_mpn = _normalize_mpn(product.mpn)
    obs_mpn = _normalize_mpn(observation.mpn)

    if product_mpn is None or obs_mpn is None:
        return None

    if product_mpn == obs_mpn:
        return MatchResult(
            matched=True,
            confidence=CONFIDENCE_MPN_EXACT,
            method="mpn_exact",
            reason="Product and observation share the same MPN.",
        )
    return MatchResult(
        matched=False,
        confidence=CONFIDENCE_NONE,
        method="mpn_mismatch",
        reason="Product and observation have a different MPN.",
    )


def _match_by_listing(
    observation: ProductObservation, expected_listing: Listing
) -> MatchResult | None:
    expected_merchant_name = _normalize_name(expected_listing.merchant.name)
    obs_merchant_name = _normalize_name(observation.merchant)

    if expected_merchant_name != obs_merchant_name:
        return MatchResult(
            matched=False,
            confidence=CONFIDENCE_NONE,
            method="merchant_mismatch",
            reason=(
                f"Observation merchant {observation.merchant!r} does not match the "
                f"expected listing's merchant {expected_listing.merchant.name!r}."
            ),
        )

    expected_external_id = expected_listing.external_id
    obs_external_id = observation.external_id
    if not expected_external_id or not obs_external_id:
        return None

    if expected_external_id.strip() == obs_external_id.strip():
        return MatchResult(
            matched=True,
            confidence=CONFIDENCE_EXTERNAL_ID_EXACT,
            method="external_id_exact",
            reason="Observation external_id matches the expected listing's external_id.",
        )
    return MatchResult(
        matched=False,
        confidence=CONFIDENCE_NONE,
        method="external_id_mismatch",
        reason="Observation external_id does not match the expected listing's external_id.",
    )


def _match_by_name(product: Product, observation: ProductObservation) -> MatchResult:
    product_name = _normalize_name(product.name)
    obs_name = _normalize_name(observation.name)

    if not product_name or not obs_name:
        return MatchResult(
            matched=False,
            confidence=CONFIDENCE_NONE,
            method="insufficient_data",
            reason="Not enough data on either side to compare.",
        )

    if product_name == obs_name:
        return MatchResult(
            matched=True,
            confidence=CONFIDENCE_NAME_EXACT,
            method="name_exact_normalized",
            reason=(
                "Product and observation names match after normalization, "
                "but no strong identifier is available."
            ),
        )

    ratio = SequenceMatcher(None, product_name, obs_name).ratio()
    if ratio >= _NAME_SIMILARITY_THRESHOLD:
        return MatchResult(
            matched=False,
            confidence=min(CONFIDENCE_NAME_APPROXIMATE_MAX, int(ratio * 30)),
            method="name_approximate",
            reason=(
                f"Product and observation names are similar (ratio={ratio:.2f}) "
                "but not identical; no strong identifier available."
            ),
        )

    return MatchResult(
        matched=False,
        confidence=CONFIDENCE_NONE,
        method="no_match",
        reason="No identifier or name signal indicates a match.",
    )


def _normalize_gtin(value: str | None) -> str | None:
    if value is None:
        return None
    digits = _NON_DIGIT_RE.sub("", value)
    if not digits:
        return None
    return digits.zfill(14)


def _normalize_mpn(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    return normalized or None


def _normalize_name(value: str | None) -> str:
    if value is None:
        return ""
    lowered = value.strip().lower()
    no_punctuation = _NON_WORD_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", no_punctuation).strip()
