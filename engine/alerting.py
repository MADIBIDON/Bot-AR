"""Opportunity Intelligence, Phase 31: turns an already-ranked opportunity
into (a) a 4-level alert tier Discord/notification logic acts on and (b)
a small set of machine-readable reason codes explaining why.

Pure, deterministic, no I/O — same guarantees as engine/opportunity.py and
engine/ranking.py. Deliberately does not recompute or duplicate anything
engine/ranking.py already scored: every function here takes an already-
built RankedOpportunity (and, for reason codes, the OpportunityCandidate
it was built from) and only adds a thin interpretation layer on top.

Why a 4-level AlertTier instead of reusing engine.ranking.Priority (5
levels: IGNORE/LOW/MEDIUM/HIGH/TOP) directly: the ranking engine's 5 tiers
are a general-purpose CLI ranking display (scripts/watch.py opportunities)
that predates this phase and stays untouched. Discord notification policy
only needs 4 buckets (IGNORE/WATCH/HIGH/URGENT — see the Phase 31 spec),
so alert_tier_for() maps the existing Priority onto them (LOW and MEDIUM
both become WATCH) rather than inventing a second, parallel set of
score thresholds that could drift from the first.

NEEDS_MARKET_DATA is its own tier, not IGNORE: engine.ranking already
marks a candidate with no resale estimate as excluded/IGNORE for ranking
purposes (it can't be scored or sorted against priced candidates), but
that is a "not enough information yet" state, never a verdict that the
opportunity is bad — a real stock event on such a listing must still be
reported (see notifications/discord/formatter.py's MARKET DATA MISSING
handling), and the listing must stay monitored. Only a genuine "no resale
estimate" exclusion maps here; an "out of stock" exclusion (only produced
when RankingConfig.exclude_out_of_stock=True, off by default) still maps
to IGNORE — that one really is a verdict, just about availability, not
data completeness.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from engine.ranking import Priority

if TYPE_CHECKING:
    from engine.ranking import OpportunityCandidate, RankedOpportunity, RankingConfig

_NO_RESALE_ESTIMATE_REASON = "no resale estimate available"
_OUT_OF_STOCK_REASON = "out of stock"


class AlertTier(StrEnum):
    IGNORE = "ignore"
    NEEDS_MARKET_DATA = "needs_market_data"
    WATCH = "watch"
    HIGH = "high"
    URGENT = "urgent"


_PRIORITY_TO_ALERT_TIER: dict[Priority, AlertTier] = {
    Priority.IGNORE: AlertTier.IGNORE,
    Priority.LOW: AlertTier.WATCH,
    Priority.MEDIUM: AlertTier.WATCH,
    Priority.HIGH: AlertTier.HIGH,
    Priority.TOP: AlertTier.URGENT,
}

# Tiers that warrant a real-time Discord notification (as opposed to
# WATCH, which is journal/digest-only per the spec) — used both here and
# by callers that need to ask "is this alert-worthy right now".
NOTIFIABLE_TIERS = (AlertTier.HIGH, AlertTier.URGENT)


def alert_tier_for(ranked: RankedOpportunity) -> AlertTier:
    if ranked.excluded:
        if ranked.exclusion_reason == _NO_RESALE_ESTIMATE_REASON:
            return AlertTier.NEEDS_MARKET_DATA
        return AlertTier.IGNORE
    return _PRIORITY_TO_ALERT_TIER[ranked.priority]


# Reason codes — plain string constants (not a StrEnum) so a Discord embed
# or a log line can join them directly (`", ".join(reason_codes)`) without
# an extra `.value` on every element.
NEEDS_MARKET_DATA = "NEEDS_MARKET_DATA"
OUT_OF_STOCK = "OUT_OF_STOCK"
NEGATIVE_NET_PROFIT = "NEGATIVE_NET_PROFIT"
LOW_MATCH_CONFIDENCE = "LOW_MATCH_CONFIDENCE"
HIGH_ROI = "HIGH_ROI"
GOOD_NET_PROFIT = "GOOD_NET_PROFIT"
HIGH_MARGIN = "HIGH_MARGIN"
HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
LOW_CONFIDENCE = "LOW_CONFIDENCE"
GOOD_MARKET_SAMPLE = "GOOD_MARKET_SAMPLE"
# Reserved, never emitted this phase: no integrated market-data source
# reports confirmed sales counts or sell-through (eBay's Browse API is
# active-listings-only — see market_data/ebay.py's own module docstring).
# Kept as a named constant so the day a sold-data source exists, the
# reason-code vocabulary doesn't need to change, only this function's
# body — see engine/ranking.py and this module's own docstring on why no
# liquidity score is fabricated in the meantime.
FAST_SELL_THROUGH = "FAST_SELL_THROUGH"

_LOW_MATCH_CONFIDENCE_THRESHOLD = 60


def compute_reason_codes(
    candidate: OpportunityCandidate,
    ranked: RankedOpportunity,
    ranking_config: RankingConfig,
) -> tuple[str, ...]:
    """Machine-readable explanation for a RankedOpportunity's score/tier.
    Thresholds are read from the same RankingConfig the score itself was
    computed with (its normalization caps: the ROI/profit/margin value
    that already earns a full 100 sub-score) — never a second, freestanding
    set of numbers that could silently drift from what actually got
    scored."""
    if ranked.excluded:
        if ranked.exclusion_reason == _NO_RESALE_ESTIMATE_REASON:
            return (NEEDS_MARKET_DATA,)
        if ranked.exclusion_reason == _OUT_OF_STOCK_REASON:
            return (OUT_OF_STOCK,)
        return ()

    codes: list[str] = []
    if not candidate.in_stock:
        codes.append(OUT_OF_STOCK)
    if candidate.net_profit is not None and candidate.net_profit < Decimal("0"):
        codes.append(NEGATIVE_NET_PROFIT)
    if candidate.match_confidence < _LOW_MATCH_CONFIDENCE_THRESHOLD:
        codes.append(LOW_MATCH_CONFIDENCE)
    if (
        candidate.roi_pct is not None
        and candidate.roi_pct >= ranking_config.roi_normalization_cap_pct
    ):
        codes.append(HIGH_ROI)
    if (
        candidate.net_profit is not None
        and candidate.net_profit >= ranking_config.profit_normalization_cap
    ):
        codes.append(GOOD_NET_PROFIT)
    if (
        candidate.net_margin_pct is not None
        and candidate.net_margin_pct >= ranking_config.margin_normalization_cap_pct
    ):
        codes.append(HIGH_MARGIN)
    if candidate.resale_confidence == "high":
        codes.append(HIGH_CONFIDENCE)
    elif candidate.resale_confidence == "low":
        codes.append(LOW_CONFIDENCE)
    if candidate.market_sample_size is not None and candidate.market_sample_size >= 8:
        codes.append(GOOD_MARKET_SAMPLE)
    return tuple(codes)


@dataclass(frozen=True, slots=True)
class OpportunityIntelligence:
    """The single, composed result of Phase 31's pipeline for one
    candidate — score/priority/financials/confidence/reason_codes in one
    place, per the spec's `evaluate_opportunity(...)` interface. Built by
    app/opportunity_snapshot.py::evaluate_opportunity_intelligence(),
    which is the actual entrypoint (it needs a Session to build the
    candidate first) — this module stays pure and Session-free, so the
    dataclass itself lives here, next to the pure functions that fill it
    in from an already-ranked candidate."""

    alert_tier: AlertTier
    reason_codes: tuple[str, ...]
    score: Decimal
    priority: Priority
    net_profit: Decimal | None
    roi_pct: Decimal | None
    net_margin_pct: Decimal | None
    estimated_resale_price: Decimal | None
    resale_confidence: str | None
    resale_price_source: str | None
    match_confidence: int
    market_sample_size: int | None
    in_stock: bool


def build_opportunity_intelligence(
    candidate: OpportunityCandidate,
    ranked: RankedOpportunity,
    ranking_config: RankingConfig,
) -> OpportunityIntelligence:
    return OpportunityIntelligence(
        alert_tier=alert_tier_for(ranked),
        reason_codes=compute_reason_codes(candidate, ranked, ranking_config),
        score=ranked.score,
        priority=ranked.priority,
        net_profit=candidate.net_profit,
        roi_pct=candidate.roi_pct,
        net_margin_pct=candidate.net_margin_pct,
        estimated_resale_price=candidate.estimated_resale_price,
        resale_confidence=candidate.resale_confidence,
        resale_price_source=candidate.resale_price_source,
        match_confidence=candidate.match_confidence,
        market_sample_size=candidate.market_sample_size,
        in_stock=candidate.in_stock,
    )
