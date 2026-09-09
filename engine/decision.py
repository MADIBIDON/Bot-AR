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
    5. price <= target_price, when target_price is set — checked *after*
       max_price so a price that blows through both is reported as
       PRICE_ABOVE_MAX (the more fundamental violation), matching the
       target=60/max=65/price=70 example.
    6. observation.available is True.

Passing all of the above -> ALLOW.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database.models import WatchRule
    from products.matcher import MatchResult
    from products.observation import ProductObservation

MIN_FINANCIAL_MATCH_CONFIDENCE = 80


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

    if watch_rule.max_price is not None and observation.price > watch_rule.max_price:
        return _reject(
            watch_rule.id,
            DecisionCode.PRICE_ABOVE_MAX,
            (
                f"Observed price {observation.price} {observation.currency} exceeds "
                f"configured max price {watch_rule.max_price} {observation.currency}."
            ),
            details={"price": observation.price, "max_price": watch_rule.max_price},
        )

    if watch_rule.target_price is not None and observation.price > watch_rule.target_price:
        return _reject(
            watch_rule.id,
            DecisionCode.TARGET_NOT_REACHED,
            (
                f"Observed price {observation.price} {observation.currency} is above "
                f"target price {watch_rule.target_price} {observation.currency}."
            ),
            details={"price": observation.price, "target_price": watch_rule.target_price},
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
