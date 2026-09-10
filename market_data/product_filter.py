"""Reject obviously-wrong market comparisons — not a full taxonomy.

products/matcher.py answers "is this the exact same product from a known
merchant" using EAN/MPN/external_id — none of that exists for a free-text
eBay listing title, so that matcher genuinely doesn't fit here (as
anticipated: "ne force pas engine.matcher si son modèle n'est pas
adapté"). This is a deliberately looser, separate check: does the
candidate's title look like a plausible comp for our product, using
whatever our own product name already encodes (language tag, product
type) plus a simple word-overlap floor.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[a-z0-9]+")

_LANGUAGE_TAGS = {"fr", "en", "jp", "jap", "us", "de", "es", "it"}

_PRODUCT_TYPE_MARKERS: dict[str, tuple[str, ...]] = {
    "etb": ("etb", "elite trainer box"),
    "display": ("display", "booster box", "booster display"),
    "booster bundle": ("booster bundle",),
    "coffret": ("coffret", "collection box", "collector chest"),
    "blister": ("blister",),
    "deck": ("deck",),
    "tin": ("tin",),
}

MIN_TOKEN_OVERLAP_RATIO = 0.3


def _normalize(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def _detect_language_tag(normalized_name: str) -> str | None:
    tokens = normalized_name.split()
    return next((token for token in tokens if token in _LANGUAGE_TAGS), None)


def _detect_product_type(normalized_name: str) -> str | None:
    for type_name, markers in _PRODUCT_TYPE_MARKERS.items():
        if any(marker in normalized_name for marker in markers):
            return type_name
    return None


def is_comparable(reference_name: str, candidate_name: str) -> bool:
    """Conservative: only rejects when both sides carry a *detected*
    signal that actively conflicts (e.g. reference says FR, candidate
    says JP; reference is an ETB, candidate is a Display). Absence of a
    signal on the candidate side is not treated as a conflict — we don't
    have enough information to reject on that alone.
    """
    ref_norm = _normalize(reference_name)
    cand_norm = _normalize(candidate_name)

    ref_lang = _detect_language_tag(ref_norm)
    if ref_lang is not None:
        cand_lang = _detect_language_tag(cand_norm)
        if cand_lang is not None and cand_lang != ref_lang:
            return False

    ref_type = _detect_product_type(ref_norm)
    if ref_type is not None:
        cand_type = _detect_product_type(cand_norm)
        if cand_type is not None and cand_type != ref_type:
            return False

    ref_tokens = set(ref_norm.split())
    if not ref_tokens:
        return True
    cand_tokens = set(cand_norm.split())
    overlap_ratio = len(ref_tokens & cand_tokens) / len(ref_tokens)
    return overlap_ratio >= MIN_TOKEN_OVERLAP_RATIO
