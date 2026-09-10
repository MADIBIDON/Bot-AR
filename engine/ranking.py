"""Opportunity Ranking: compares already-scored opportunities and orders
them by how worth acting on they are.

Pure, deterministic, no I/O, no Discord, no purchase logic — same
guarantees as engine/opportunity.py and engine/decision.py. Takes a list
of OpportunityCandidate (one per WatchRule, already carrying whatever
engine/opportunity.py + market_data/ already computed) and produces a
sorted list[RankedOpportunity] with an explainable 0-100 score.

Score is kept as Decimal, not float, for the same reason the rest of the
app uses Decimal for money: every input that feeds it (profit, ROI,
margin) is already Decimal, and a deterministic/testable score should
never pick up float rounding noise. It is not a monetary value itself,
but nothing here benefits from float's extra range or speed at this
scale (dozens of candidates, not millions).

No ML: the score is a fixed weighted average of a handful of 0-100
sub-scores (see score_breakdown on the result), each computed by a
simple, documented, deterministic rule below.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

_HUNDRED = Decimal("100")
_ZERO = Decimal("0")
_SCORE_PRECISION = Decimal("0.01")

_RESALE_CONFIDENCE_SCORES: dict[str, Decimal] = {
    "high": Decimal("100"),
    "medium": Decimal("60"),
    "low": Decimal("20"),
}
# A manual price (resale_confidence=None) carries no market-derived
# confidence signal, but it is not "unknown" either — the user explicitly
# vouches for it. Scoring it as full confidence keeps manual-mode
# opportunities comparable to market-mode ones instead of being
# systematically capped below them for lacking a signal they were never
# meant to produce.
_MANUAL_RESALE_CONFIDENCE_SCORE = Decimal("100")


class Priority(StrEnum):
    IGNORE = "ignore"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    TOP = "top"


@dataclass(frozen=True, slots=True)
class OpportunityCandidate:
    """One WatchRule's current opportunity, already computed upstream by
    engine.opportunity (and, for market mode, market_data.estimator) —
    this module never fetches or recomputes any of it.

    net_profit/roi_pct/net_margin_pct/estimated_resale_price are None
    together exactly when no resale estimate exists yet (manual field
    unset, or market data unavailable) — that candidate is then
    excluded, not scored as if profit were zero.

    resale_confidence is None specifically for a manually-supplied resale
    price (WatchRule.resale_price_mode == "manual"), which carries no
    market-derived confidence signal at all — see _score_candidate for how
    that is scored (deliberately not the same as a "low" market estimate).
    """

    watch_rule_id: int
    merchant: str
    product_name: str
    purchase_price: Decimal
    estimated_resale_price: Decimal | None
    net_profit: Decimal | None
    roi_pct: Decimal | None
    net_margin_pct: Decimal | None
    resale_confidence: str | None  # "low" | "medium" | "high", or None for manual mode
    in_stock: bool
    match_confidence: int
    market_sample_size: int | None = None


@dataclass(frozen=True, slots=True)
class RankingConfig:
    """Relative weights for each scoring dimension — only their ratio to
    each other matters, they don't need to sum to any particular total
    (rank_opportunities normalizes by their sum). All must be >= 0, and
    at least one must be > 0.

    The *_normalization_cap fields say what raw value already earns the
    full 100 sub-score (values above the cap are clamped, not extrapolated
    further) — e.g. the default roi_normalization_cap_pct=100 means a
    100% ROI (profit == purchase price) already maxes out the ROI
    sub-score; a 300% ROI scores the same 100, it doesn't break the 0-100
    bound. Negative values always clamp to a sub-score of 0, never
    negative — a bad candidate should score low, not "more low than 0".
    """

    roi_weight: Decimal = Decimal("30")
    profit_weight: Decimal = Decimal("20")
    margin_weight: Decimal = Decimal("15")
    resale_confidence_weight: Decimal = Decimal("15")
    match_confidence_weight: Decimal = Decimal("15")
    sample_size_weight: Decimal = Decimal("5")

    roi_normalization_cap_pct: Decimal = Decimal("100")
    profit_normalization_cap: Decimal = Decimal("100")
    margin_normalization_cap_pct: Decimal = Decimal("50")

    exclude_out_of_stock: bool = False
    out_of_stock_penalty_pct: Decimal = Decimal("50")
    negative_profit_score_cap: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        weights = (
            self.roi_weight,
            self.profit_weight,
            self.margin_weight,
            self.resale_confidence_weight,
            self.match_confidence_weight,
            self.sample_size_weight,
        )
        if any(w < 0 for w in weights):
            raise ValueError("weights must not be negative")
        if sum(weights, _ZERO) <= 0:
            raise ValueError("at least one weight must be > 0")
        for name, cap in (
            ("roi_normalization_cap_pct", self.roi_normalization_cap_pct),
            ("profit_normalization_cap", self.profit_normalization_cap),
            ("margin_normalization_cap_pct", self.margin_normalization_cap_pct),
        ):
            if cap <= 0:
                raise ValueError(f"{name} must be > 0")
        if not (0 <= self.out_of_stock_penalty_pct <= 100):
            raise ValueError("out_of_stock_penalty_pct must be between 0 and 100")
        if not (0 <= self.negative_profit_score_cap <= 100):
            raise ValueError("negative_profit_score_cap must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class RankingThresholds:
    """Minimum score (inclusive) required for each priority tier, most
    exclusive first. Below low_min -> IGNORE."""

    top_min: Decimal = Decimal("85")
    high_min: Decimal = Decimal("65")
    medium_min: Decimal = Decimal("40")
    low_min: Decimal = Decimal("15")

    def __post_init__(self) -> None:
        if not (self.top_min >= self.high_min >= self.medium_min >= self.low_min >= 0):
            raise ValueError(
                "thresholds must satisfy top_min >= high_min >= medium_min >= low_min >= 0"
            )


@dataclass(frozen=True, slots=True)
class RankedOpportunity:
    watch_rule_id: int
    merchant: str
    product_name: str
    purchase_price: Decimal
    estimated_resale_price: Decimal | None
    net_profit: Decimal | None
    roi_pct: Decimal | None
    net_margin_pct: Decimal | None
    score: Decimal
    priority: Priority
    rank: int
    score_breakdown: dict[str, Decimal] = field(default_factory=dict)
    excluded: bool = False
    exclusion_reason: str | None = None
    notes: tuple[str, ...] = ()


def _clamp(value: Decimal, *, low: Decimal = _ZERO, high: Decimal = _HUNDRED) -> Decimal:
    return max(low, min(high, value))


def _normalized_score(value: Decimal, cap: Decimal) -> Decimal:
    if value <= 0:
        return _ZERO
    return _clamp((value / cap) * _HUNDRED)


def _sample_size_score(sample_size: int | None) -> Decimal:
    if sample_size is None:
        return _ZERO
    if sample_size >= 8:
        return Decimal("100")
    if sample_size >= 3:
        return Decimal("60")
    if sample_size >= 1:
        return Decimal("30")
    return _ZERO


def _round_score(value: Decimal) -> Decimal:
    return value.quantize(_SCORE_PRECISION, rounding=ROUND_HALF_UP)


def _priority_for(score: Decimal, thresholds: RankingThresholds) -> Priority:
    if score >= thresholds.top_min:
        return Priority.TOP
    if score >= thresholds.high_min:
        return Priority.HIGH
    if score >= thresholds.medium_min:
        return Priority.MEDIUM
    if score >= thresholds.low_min:
        return Priority.LOW
    return Priority.IGNORE


def _score_candidate(
    candidate: OpportunityCandidate, config: RankingConfig
) -> tuple[Decimal, dict[str, Decimal], tuple[str, ...]]:
    breakdown = {
        "roi": _normalized_score(candidate.roi_pct, config.roi_normalization_cap_pct),
        "profit": _normalized_score(candidate.net_profit, config.profit_normalization_cap),
        "margin": _normalized_score(candidate.net_margin_pct, config.margin_normalization_cap_pct),
        "resale_confidence": (
            _MANUAL_RESALE_CONFIDENCE_SCORE
            if candidate.resale_confidence is None
            else _RESALE_CONFIDENCE_SCORES.get(candidate.resale_confidence, _ZERO)
        ),
        "match_confidence": _clamp(Decimal(candidate.match_confidence)),
        "sample_size": _sample_size_score(candidate.market_sample_size),
    }
    weights = {
        "roi": config.roi_weight,
        "profit": config.profit_weight,
        "margin": config.margin_weight,
        "resale_confidence": config.resale_confidence_weight,
        "match_confidence": config.match_confidence_weight,
        "sample_size": config.sample_size_weight,
    }
    total_weight = sum(weights.values(), _ZERO)
    raw_score = sum((breakdown[key] * weights[key] for key in breakdown), _ZERO) / total_weight

    notes: list[str] = []
    score = raw_score

    if not candidate.in_stock and not config.exclude_out_of_stock:
        score = score * (_HUNDRED - config.out_of_stock_penalty_pct) / _HUNDRED
        notes.append("out_of_stock_penalty_applied")

    if candidate.net_profit is not None and candidate.net_profit < 0:
        capped = min(score, config.negative_profit_score_cap)
        if capped < score:
            notes.append("negative_profit_cap_applied")
        score = capped

    return _round_score(_clamp(score)), breakdown, tuple(notes)


def rank_opportunities(
    candidates: list[OpportunityCandidate],
    config: RankingConfig | None = None,
    thresholds: RankingThresholds | None = None,
) -> list[RankedOpportunity]:
    """Scores and sorts every candidate, best first.

    A candidate with no resale estimate yet (estimated_resale_price,
    net_profit, roi_pct, net_margin_pct all None) or excluded for being
    out of stock (when config.exclude_out_of_stock=True) is still
    returned — with excluded=True, score=0, priority=IGNORE, and a plain
    exclusion_reason — rather than silently dropped, so a caller can
    always account for every WatchRule it passed in.

    Ordering is fully deterministic: by score descending, then net_profit
    descending (None treated as lowest), then roi_pct descending (None
    treated as lowest), then watch_rule_id ascending — so two candidates
    that tie on every business metric always come out in the same order.
    """
    config = config or RankingConfig()
    thresholds = thresholds or RankingThresholds()

    scored: list[RankedOpportunity] = []
    for candidate in candidates:
        if candidate.in_stock is False and config.exclude_out_of_stock:
            scored.append(
                RankedOpportunity(
                    watch_rule_id=candidate.watch_rule_id,
                    merchant=candidate.merchant,
                    product_name=candidate.product_name,
                    purchase_price=candidate.purchase_price,
                    estimated_resale_price=candidate.estimated_resale_price,
                    net_profit=candidate.net_profit,
                    roi_pct=candidate.roi_pct,
                    net_margin_pct=candidate.net_margin_pct,
                    score=_ZERO,
                    priority=Priority.IGNORE,
                    rank=0,
                    excluded=True,
                    exclusion_reason="out of stock",
                )
            )
            continue

        no_estimate = (
            candidate.estimated_resale_price is None
            or candidate.net_profit is None
            or candidate.roi_pct is None
            or candidate.net_margin_pct is None
        )
        if no_estimate:
            scored.append(
                RankedOpportunity(
                    watch_rule_id=candidate.watch_rule_id,
                    merchant=candidate.merchant,
                    product_name=candidate.product_name,
                    purchase_price=candidate.purchase_price,
                    estimated_resale_price=candidate.estimated_resale_price,
                    net_profit=candidate.net_profit,
                    roi_pct=candidate.roi_pct,
                    net_margin_pct=candidate.net_margin_pct,
                    score=_ZERO,
                    priority=Priority.IGNORE,
                    rank=0,
                    excluded=True,
                    exclusion_reason="no resale estimate available",
                )
            )
            continue

        score, breakdown, notes = _score_candidate(candidate, config)
        scored.append(
            RankedOpportunity(
                watch_rule_id=candidate.watch_rule_id,
                merchant=candidate.merchant,
                product_name=candidate.product_name,
                purchase_price=candidate.purchase_price,
                estimated_resale_price=candidate.estimated_resale_price,
                net_profit=candidate.net_profit,
                roi_pct=candidate.roi_pct,
                net_margin_pct=candidate.net_margin_pct,
                score=score,
                priority=_priority_for(score, thresholds),
                rank=0,
                score_breakdown=breakdown,
                notes=notes,
            )
        )

    def _sort_key(item: RankedOpportunity) -> tuple[Decimal, Decimal, Decimal, int]:
        profit_key = item.net_profit if item.net_profit is not None else Decimal("-Infinity")
        roi_key = item.roi_pct if item.roi_pct is not None else Decimal("-Infinity")
        return (-item.score, -profit_key, -roi_key, item.watch_rule_id)

    scored.sort(key=_sort_key)

    return [replace(r, rank=rank) for rank, r in enumerate(scored, start=1)]
