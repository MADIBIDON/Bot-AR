"""Opportunity Engine: is a resale flip financially worth it?

Pure, deterministic, no I/O, no Discord, no purchase logic — same
guarantees as engine/decision.py. Given an observed purchase price and a
manually-configured resale estimate (WatchRule.estimated_resale_price and
friends), computes gross/net profit, margin, ROI, and a break-even resale
price, then classifies the result against configurable thresholds.

No StockX/eBay/Vinted lookup exists yet — deliberately: this phase
validates the arithmetic with a human-supplied resale price first. Never
triggers a purchase; the returned status is analytical only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

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
