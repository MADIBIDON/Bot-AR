"""Opportunity Engine: is a resale flip financially worth it?

Pure, deterministic, no I/O, no Discord, no purchase logic — same
guarantees as engine/decision.py. Given an observed purchase price and a
manually-configured resale estimate (WatchRule.estimated_resale_price and
friends), computes gross/net profit, margin, ROI, and a break-even resale
price, then classifies the result against configurable thresholds.

No StockX/eBay/Vinted lookup exists yet — deliberately: this phase
validates the arithmetic with a human-supplied resale price first.

Phase 25: recommend_purchase() turns that classification plus a resale
Confidence into the STRONG_BUY/BUY/ALERT_ONLY/REJECT label both
app/notify.py (for the Discord embed) and purchase/engine.py (for the
actual auto-buy gate) need — kept here, a peer of engine/decision.py,
rather than in app/ or purchase/, specifically so both can share it
without either importing the other (purchase/ must never depend on
app/). It imports market_data.estimator.Confidence — a small,
dependency-free StrEnum with no I/O of its own — but never anything that
performs a market lookup; that stays app/resale.py's job. The status this
module returns remains purely analytical: nothing here ever triggers a
purchase on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from engine.decision import (
    effective_fixed_fee,
    effective_minimum_net_profit,
    effective_minimum_roi_pct,
    effective_other_costs,
    effective_platform_fee_pct,
    effective_shipping_cost,
)
from market_data.estimator import Confidence

if TYPE_CHECKING:
    from database.models import WatchRule

_CENTS = Decimal("0.01")
_HUNDRED = Decimal("100")


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _round_pct(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


class OpportunityStatus(StrEnum):
    REJECT = "reject"
    WATCH = "watch"
    BUY_CANDIDATE = "buy_candidate"
    STRONG_BUY_CANDIDATE = "strong_buy_candidate"


@dataclass(frozen=True, slots=True)
class OpportunityConfig:
    """Manually-supplied resale assumptions for one WatchRule."""

    estimated_resale_price: Decimal | None = None
    platform_fee_pct: Decimal = Decimal("0")
    fixed_fee: Decimal = Decimal("0")
    shipping_cost: Decimal = Decimal("0")
    other_costs: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class OpportunityThresholds:
    """Classification thresholds — never hardcoded inside the engine."""

    min_net_profit: Decimal = Decimal("0")
    min_roi_pct: Decimal = Decimal("0")
    min_margin_pct: Decimal = Decimal("0")
    strong_buy_roi_pct: Decimal = Decimal("50")


def build_opportunity_inputs(
    watch_rule: WatchRule, resale_price: Decimal
) -> tuple[OpportunityConfig, OpportunityThresholds]:
    """Builds (config, thresholds) from a Product Watch's effective (Phase
    22 rule-then-Product fallback) fee/shipping/threshold fields plus an
    already-resolved resale price — the one place this assembly happens,
    reused by app/notify.py (item price only, for the immediate alert)
    and purchase/engine.py (the real revalidated total, for the
    post-checkout-preparation profitability re-check) so the two never
    drift apart."""
    config = OpportunityConfig(
        estimated_resale_price=resale_price,
        platform_fee_pct=effective_platform_fee_pct(watch_rule) or Decimal("0"),
        fixed_fee=effective_fixed_fee(watch_rule) or Decimal("0"),
        shipping_cost=effective_shipping_cost(watch_rule) or Decimal("0"),
        other_costs=effective_other_costs(watch_rule) or Decimal("0"),
    )
    thresholds = OpportunityThresholds(
        min_net_profit=effective_minimum_net_profit(watch_rule) or Decimal("0"),
        min_roi_pct=effective_minimum_roi_pct(watch_rule) or Decimal("0"),
    )
    return config, thresholds


@dataclass(frozen=True, slots=True)
class OpportunityResult:
    status: OpportunityStatus
    reason: str
    purchase_price: Decimal
    estimated_resale_price: Decimal
    platform_fee_amount: Decimal
    fixed_fee: Decimal
    shipping_cost: Decimal
    other_costs: Decimal
    gross_profit: Decimal
    net_profit: Decimal
    net_margin_pct: Decimal
    roi_pct: Decimal
    break_even_resale_price: Decimal | None
    resale_safety_margin_pct: Decimal | None


def evaluate_opportunity(
    purchase_price: Decimal,
    config: OpportunityConfig,
    thresholds: OpportunityThresholds | None = None,
) -> OpportunityResult | None:
    """None when no estimated_resale_price is configured — there is
    nothing to evaluate yet, which is not the same as a rejection."""
    if config.estimated_resale_price is None:
        return None
    if purchase_price <= 0:
        raise ValueError("purchase_price must be positive to compute ROI")

    thresholds = thresholds or OpportunityThresholds()
    resale = config.estimated_resale_price

    platform_fee_amount = resale * config.platform_fee_pct / _HUNDRED
    total_fees = platform_fee_amount + config.fixed_fee + config.shipping_cost + config.other_costs

    gross_profit = resale - purchase_price
    net_profit = gross_profit - total_fees

    net_margin_pct = (net_profit / resale) * _HUNDRED
    roi_pct = (net_profit / purchase_price) * _HUNDRED

    fee_fraction = config.platform_fee_pct / _HUNDRED
    if fee_fraction < 1:
        fixed_costs = purchase_price + config.fixed_fee + config.shipping_cost + config.other_costs
        break_even_resale_price = fixed_costs / (Decimal("1") - fee_fraction)
        safety_margin_pct = ((resale - break_even_resale_price) / resale) * _HUNDRED
    else:
        # A platform fee of 100%+ of the resale price makes break-even
        # mathematically undefined (no finite resale price recovers
        # costs) — an honest None, not a crash or a fabricated number.
        break_even_resale_price = None
        safety_margin_pct = None

    status, reason = _classify(net_profit, roi_pct, net_margin_pct, thresholds)

    return OpportunityResult(
        status=status,
        reason=reason,
        purchase_price=_round_money(purchase_price),
        estimated_resale_price=_round_money(resale),
        platform_fee_amount=_round_money(platform_fee_amount),
        fixed_fee=_round_money(config.fixed_fee),
        shipping_cost=_round_money(config.shipping_cost),
        other_costs=_round_money(config.other_costs),
        gross_profit=_round_money(gross_profit),
        net_profit=_round_money(net_profit),
        net_margin_pct=_round_pct(net_margin_pct),
        roi_pct=_round_pct(roi_pct),
        break_even_resale_price=(
            _round_money(break_even_resale_price) if break_even_resale_price is not None else None
        ),
        resale_safety_margin_pct=(
            _round_pct(safety_margin_pct) if safety_margin_pct is not None else None
        ),
    )


def _classify(
    net_profit: Decimal,
    roi_pct: Decimal,
    net_margin_pct: Decimal,
    thresholds: OpportunityThresholds,
) -> tuple[OpportunityStatus, str]:
    if net_profit < thresholds.min_net_profit:
        return (
            OpportunityStatus.REJECT,
            f"Net profit {net_profit} is below the minimum required {thresholds.min_net_profit}.",
        )
    if roi_pct < thresholds.min_roi_pct or net_margin_pct < thresholds.min_margin_pct:
        return (
            OpportunityStatus.WATCH,
            (
                f"Profitable but below buy thresholds (roi={roi_pct}% vs "
                f"min {thresholds.min_roi_pct}%, margin={net_margin_pct}% vs "
                f"min {thresholds.min_margin_pct}%)."
            ),
        )
    if roi_pct >= thresholds.strong_buy_roi_pct:
        return (
            OpportunityStatus.STRONG_BUY_CANDIDATE,
            f"ROI {roi_pct}% meets the strong-buy threshold {thresholds.strong_buy_roi_pct}%.",
        )
    return (
        OpportunityStatus.BUY_CANDIDATE,
        "Meets the configured net profit, ROI, and margin thresholds.",
    )


class PurchaseRecommendation(StrEnum):
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    ALERT_ONLY = "alert_only"
    REJECT = "reject"


_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


def recommend_purchase(
    opportunity: OpportunityResult | None,
    resale_confidence: Confidence,
    minimum_resale_confidence: str | None,
) -> tuple[PurchaseRecommendation, str]:
    """Translates this module's pure financial classification plus how
    much we trust the resale number into the one label Discord shows and
    purchase/engine.py gates auto-buy on.

    No opportunity computed at all (no resale price known) -> ALERT_ONLY:
    there is a genuine restock to report, just nothing to judge
    profitability by yet. REJECT/WATCH statuses stay REJECT/ALERT_ONLY
    regardless of confidence — a bad number doesn't need a confidence
    check to already be a reject. A profitable result
    (BUY_CANDIDATE/STRONG_BUY_CANDIDATE) is only ever promoted to
    BUY/STRONG_BUY when resale_confidence clears minimum_resale_confidence
    (defaulting to requiring at least MEDIUM when nothing was configured)
    — otherwise it is downgraded to ALERT_ONLY: profitable on paper, but
    not confident enough to trust with real money.
    """
    if opportunity is None:
        return PurchaseRecommendation.ALERT_ONLY, "No resale estimate available yet."

    if opportunity.status == OpportunityStatus.REJECT:
        return PurchaseRecommendation.REJECT, opportunity.reason

    if opportunity.status == OpportunityStatus.WATCH:
        return PurchaseRecommendation.ALERT_ONLY, opportunity.reason

    required = (
        Confidence(minimum_resale_confidence) if minimum_resale_confidence else Confidence.MEDIUM
    )
    if _CONFIDENCE_RANK[resale_confidence] < _CONFIDENCE_RANK[required]:
        return (
            PurchaseRecommendation.ALERT_ONLY,
            (
                f"Profitable ({opportunity.reason}) but resale confidence "
                f"{resale_confidence.value} is below the required {required.value} — "
                "too uncertain to auto-buy."
            ),
        )

    if opportunity.status == OpportunityStatus.STRONG_BUY_CANDIDATE:
        return PurchaseRecommendation.STRONG_BUY, opportunity.reason
    return PurchaseRecommendation.BUY, opportunity.reason
