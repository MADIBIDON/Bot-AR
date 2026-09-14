"""Decision Engine: does an observation satisfy a WatchRule's rules?

Deterministic, pure, no I/O: takes an already-fetched WatchRule,
ProductObservation, and MatchResult, and answers allow/reject with an
explicit, machine-readable decision_code. Detection ("what changed") lives
in engine/change_detection.py; action (notify, buy) is a later phase. This
layer only answers "does this state comply with the rule", not "what do we
do about it" — and says nothing about auto-buy, budget, or risk, which are
separate, later engines.

Checks run in this order, first failure wins:
    1. WatchRule.enabled
    2. Observation sanity — defensive only. ProductObservation already
       guarantees non-empty fields, price >= 0, and a UTC-aware timestamp
       at construction (Phase 4), so this is essentially a guard against a
       caller bug, not a real business rule.
    3. Product match strong enough for a financial decision: matched is
       False, OR confidence < MIN_FINANCIAL_MATCH_CONFIDENCE. Both cases
       share one code (PRODUCT_MISMATCH) — no downstream consumer would act
       differently on "wrong product" vs. "right product, not confident
       enough"; the nuance lives in `reason`.
    4. price <= max_price, when max_price is set — an absolute ceiling.
       Still enforced even in profitability mode (Phase 25) — when set,
       it is an explicit hard cap the user chose to keep; leaving it unset
       is how profitability mode goes uncapped on price alone.
    5. price <= target_price, when target_price is set — checked *after*
       max_price so a price that blows through both is reported as
       PRICE_ABOVE_MAX (the more fundamental violation), matching the
       target=60/max=65/price=70 example. Skipped entirely in
       profitability mode (Phase 25): target_price there is a "target buy
       price" reference shown in the opportunity breakdown, not a
       notification gate — a restock above it can still be an excellent,
       alertable opportunity if the margin justifies the extra spend.
    6. observation.available is True.

Passing all of the above -> ALLOW.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from database.models import WatchRule
    from products.matcher import MatchResult
    from products.observation import ProductObservation

MIN_FINANCIAL_MATCH_CONFIDENCE = 80


def _effective(value: object, watch_rule: WatchRule, attr: str) -> object:
    """Shared fallback: a rule's own value wins when set, else the
    parent Product's value, else None — the same rule effective_max_price
    established in Phase 22, generalized (Phase 25) for every other
    dual-homed field below rather than hand-writing the same three lines
    for each one. `product` is only dereferenced when actually needed, so
    a bare, unpersisted WatchRule with no `product` set (as many unit
    tests construct) never crashes as long as it also sets its own value."""
    if value is not None:
        return value
    return getattr(watch_rule.product, attr, None) if watch_rule.product is not None else None


def effective_max_price(watch_rule: WatchRule) -> Decimal | None:
    """Phase 22: a WatchRule created by product-watch discovery has no
    max_price of its own — every Listing/WatchRule under one product
    watch shares the product's single ceiling instead, so a later change
    to it is picked up everywhere at once. A rule with its own explicit
    max_price (the pre-Phase-22 WatchRules, or any rule created directly)
    always wins — never overridden by the product's value."""
    return _effective(watch_rule.max_price, watch_rule, "max_price")


def effective_target_price(watch_rule: WatchRule) -> Decimal | None:
    return _effective(watch_rule.target_price, watch_rule, "target_price")


def effective_platform_fee_pct(watch_rule: WatchRule) -> Decimal | None:
    return _effective(watch_rule.platform_fee_pct, watch_rule, "platform_fee_pct")


def effective_fixed_fee(watch_rule: WatchRule) -> Decimal | None:
    return _effective(watch_rule.fixed_fee, watch_rule, "fixed_fee")


def effective_shipping_cost(watch_rule: WatchRule) -> Decimal | None:
    return _effective(watch_rule.shipping_cost, watch_rule, "shipping_cost")


def effective_other_costs(watch_rule: WatchRule) -> Decimal | None:
    return _effective(watch_rule.other_costs, watch_rule, "other_costs")


def effective_estimated_resale_price(watch_rule: WatchRule) -> Decimal | None:
    return _effective(watch_rule.estimated_resale_price, watch_rule, "estimated_resale_price")


def effective_resale_price_mode(watch_rule: WatchRule) -> str:
    """Unlike the other fields here, resale_price_mode is NOT NULL on
    both tables (default "manual"), so a rule's own value always wins —
    "manual" from a fresh WatchRule would otherwise incorrectly shadow an
    explicit "market" configured on the Product. None is treated the same
    as "manual": a column default only actually lands in this attribute
    once a row is inserted, so a transient, never-persisted WatchRule (as
    plenty of unit tests construct) reads back as None here, not
    "manual" — without this, such a rule could never see its Product's
    "market" mode either."""
    if watch_rule.resale_price_mode not in (None, "manual"):
        return watch_rule.resale_price_mode
    if watch_rule.product is not None and watch_rule.product.resale_price_mode == "market":
        return "market"
    return watch_rule.resale_price_mode or "manual"


def effective_market_source(watch_rule: WatchRule) -> str | None:
    return _effective(watch_rule.market_source, watch_rule, "market_source")


def effective_minimum_net_profit(watch_rule: WatchRule) -> Decimal | None:
    return _effective(watch_rule.minimum_net_profit, watch_rule, "minimum_net_profit")


def effective_minimum_roi_pct(watch_rule: WatchRule) -> Decimal | None:
    return _effective(watch_rule.minimum_roi_pct, watch_rule, "minimum_roi_pct")


def effective_minimum_resale_confidence(watch_rule: WatchRule) -> str | None:
    return _effective(watch_rule.minimum_resale_confidence, watch_rule, "minimum_resale_confidence")


def effective_estimated_resale_trusted(watch_rule: WatchRule) -> bool:
    if watch_rule.estimated_resale_trusted:
        return True
    return bool(watch_rule.product is not None and watch_rule.product.estimated_resale_trusted)


def effective_resale_updated_at(watch_rule: WatchRule) -> datetime | None:
    """Phase 33 section 21 — see database/models.py's WatchRule/Product
    resale_updated_at comments and purchase/engine.py's staleness gate."""
    return _effective(watch_rule.resale_updated_at, watch_rule, "resale_updated_at")


def is_profitability_mode(watch_rule: WatchRule) -> bool:
    """True once a Product Watch (or a rule directly) has configured a
    profitability threshold — see the module docstring's item 5. This is
    the one switch that changes evaluate()'s notification policy from
    "hard price ceiling" to "profitability decides"."""
    return (
        effective_minimum_net_profit(watch_rule) is not None
        or effective_minimum_roi_pct(watch_rule) is not None
    )


class DecisionCode(StrEnum):
    ALLOW = "allow"
    RULE_DISABLED = "rule_disabled"
    INVALID_OBSERVATION = "invalid_observation"
    PRODUCT_MISMATCH = "product_mismatch"
    PRICE_ABOVE_MAX = "price_above_max"
    TARGET_NOT_REACHED = "target_not_reached"
    STOCK_UNAVAILABLE = "stock_unavailable"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    allowed: bool
    decision_code: DecisionCode
    reason: str
    rule_id: int
    details: dict[str, object] = field(default_factory=dict)


def evaluate(
    watch_rule: WatchRule,
    observation: ProductObservation,
    match_result: MatchResult,
) -> DecisionResult:
    if not watch_rule.enabled:
        return _reject(
            watch_rule.id, DecisionCode.RULE_DISABLED, f"Watch rule {watch_rule.id} is disabled."
        )

    if observation is None:
        return _reject(
            watch_rule.id, DecisionCode.INVALID_OBSERVATION, "No observation was provided."
        )

    if not match_result.matched or match_result.confidence < MIN_FINANCIAL_MATCH_CONFIDENCE:
        return _reject(
            watch_rule.id,
            DecisionCode.PRODUCT_MISMATCH,
            (
                f"Product match confidence {match_result.confidence} is below the minimum "
                f"required for a financial decision ({MIN_FINANCIAL_MATCH_CONFIDENCE}): "
                f"{match_result.reason}"
            ),
            details={"confidence": match_result.confidence, "method": match_result.method},
        )

    max_price = effective_max_price(watch_rule)
    if max_price is not None and observation.price > max_price:
        return _reject(
            watch_rule.id,
            DecisionCode.PRICE_ABOVE_MAX,
            (
                f"Observed price {observation.price} {observation.currency} exceeds "
                f"configured max price {max_price} {observation.currency}."
            ),
            details={"price": observation.price, "max_price": max_price},
        )

    target_price = effective_target_price(watch_rule)
    if (
        target_price is not None
        and observation.price > target_price
        and not is_profitability_mode(watch_rule)
    ):
        return _reject(
            watch_rule.id,
            DecisionCode.TARGET_NOT_REACHED,
            (
                f"Observed price {observation.price} {observation.currency} is above "
                f"target price {target_price} {observation.currency}."
            ),
            details={"price": observation.price, "target_price": target_price},
        )

    if not observation.available:
        return _reject(
            watch_rule.id,
            DecisionCode.STOCK_UNAVAILABLE,
            "Observation reports the product as unavailable.",
        )

    return DecisionResult(
        allowed=True,
        decision_code=DecisionCode.ALLOW,
        reason="Observation satisfies availability, product match, and price constraints.",
        rule_id=watch_rule.id,
    )


def _reject(
    rule_id: int,
    code: DecisionCode,
    reason: str,
    details: dict[str, object] | None = None,
) -> DecisionResult:
    return DecisionResult(
        allowed=False, decision_code=code, reason=reason, rule_id=rule_id, details=details or {}
    )
