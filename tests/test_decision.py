from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from database.models import WatchRule
from engine.decision import MIN_FINANCIAL_MATCH_CONFIDENCE, DecisionCode, evaluate
from products.matcher import MatchResult
from products.observation import ProductObservation


def _rule(**overrides: object) -> WatchRule:
    defaults: dict[str, object] = dict(
        id=1,
        product_id=1,
        check_interval=300,
        max_quantity=1,
        enabled=True,
        target_price=None,
        max_price=None,
    )
    defaults.update(overrides)
    return WatchRule(**defaults)  # type: ignore[arg-type]


def _observation(**overrides: object) -> ProductObservation:
    defaults: dict[str, object] = dict(
        merchant="RetailerA",
        external_id="fake-123",
        name="Duopack Evoli 30 ans",
        price=Decimal("59.99"),
        currency="EUR",
        available=True,
        url="https://a.example/p/1",
        observed_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return ProductObservation(**defaults)  # type: ignore[arg-type]


def _match(confidence: int = 100, matched: bool = True, method: str = "ean_exact") -> MatchResult:
    return MatchResult(matched=matched, confidence=confidence, method=method, reason="test")


def test_allow_when_all_conditions_met() -> None:
    result = evaluate(_rule(), _observation(), _match())
    assert result.allowed is True
    assert result.decision_code == DecisionCode.ALLOW


def test_disabled_rule_is_rejected() -> None:
    result = evaluate(_rule(enabled=False), _observation(), _match())
    assert result.allowed is False
    assert result.decision_code == DecisionCode.RULE_DISABLED


def test_unmatched_product_is_rejected() -> None:
    result = evaluate(_rule(), _observation(), _match(matched=False, confidence=0))
    assert result.allowed is False
    assert result.decision_code == DecisionCode.PRODUCT_MISMATCH


def test_confidence_50_is_rejected() -> None:
    match = _match(confidence=50, method="name_exact_normalized")
    result = evaluate(_rule(), _observation(), match)
    assert result.allowed is False
    assert result.decision_code == DecisionCode.PRODUCT_MISMATCH


def test_confidence_80_is_accepted() -> None:
    match = _match(confidence=80, method="external_id_exact")
    result = evaluate(_rule(), _observation(), match)
    assert result.allowed is True


def test_confidence_90_is_accepted() -> None:
    match = _match(confidence=90, method="mpn_exact")
    result = evaluate(_rule(), _observation(), match)
    assert result.allowed is True


def test_confidence_100_is_accepted() -> None:
    result = evaluate(_rule(), _observation(), _match(confidence=100, method="ean_exact"))
    assert result.allowed is True


def test_confidence_threshold_is_centralized_constant() -> None:
    assert MIN_FINANCIAL_MATCH_CONFIDENCE == 80


def test_price_above_max_is_rejected() -> None:
    rule = _rule(max_price=Decimal("60"))
    observation = _observation(price=Decimal("69.99"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is False
    assert result.decision_code == DecisionCode.PRICE_ABOVE_MAX


def test_price_equal_to_max_is_accepted() -> None:
    rule = _rule(max_price=Decimal("60"))
    observation = _observation(price=Decimal("60"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is True


def test_price_above_target_but_below_max_is_rejected() -> None:
    rule = _rule(target_price=Decimal("60"), max_price=Decimal("65"))
    observation = _observation(price=Decimal("62"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is False
    assert result.decision_code == DecisionCode.TARGET_NOT_REACHED


def test_price_equal_to_target_is_accepted() -> None:
    rule = _rule(target_price=Decimal("60"), max_price=Decimal("65"))
    observation = _observation(price=Decimal("60"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is True


def test_price_below_target_is_accepted() -> None:
    rule = _rule(target_price=Decimal("60"), max_price=Decimal("65"))
    observation = _observation(price=Decimal("59.99"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is True


def test_price_above_max_takes_priority_over_target_not_reached() -> None:
    rule = _rule(target_price=Decimal("60"), max_price=Decimal("65"))
    observation = _observation(price=Decimal("70"))
    result = evaluate(rule, observation, _match())
    assert result.decision_code == DecisionCode.PRICE_ABOVE_MAX


def test_no_target_price_means_no_target_constraint() -> None:
    rule = _rule(target_price=None, max_price=Decimal("65"))
    observation = _observation(price=Decimal("64"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is True


def test_no_max_price_means_no_ceiling_constraint() -> None:
    rule = _rule(target_price=None, max_price=None)
    observation = _observation(price=Decimal("999999.99"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is True


def test_unavailable_stock_is_rejected() -> None:
    result = evaluate(_rule(), _observation(available=False), _match())
    assert result.allowed is False
    assert result.decision_code == DecisionCode.STOCK_UNAVAILABLE


def test_zero_price_is_allowed_when_rules_permit() -> None:
    rule = _rule(max_price=Decimal("10"))
    observation = _observation(price=Decimal("0"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is True


def test_price_comparison_uses_decimal_exactly() -> None:
    rule = _rule(target_price=Decimal("19.99"))
    observation = _observation(price=Decimal("19.99"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is True
    assert isinstance(observation.price, Decimal)


def test_disabled_rule_takes_priority_over_bad_match() -> None:
    rule = _rule(enabled=False)
    result = evaluate(rule, _observation(), _match(matched=False, confidence=0))
    assert result.decision_code == DecisionCode.RULE_DISABLED


def test_product_mismatch_takes_priority_over_price_above_max() -> None:
    rule = _rule(max_price=Decimal("10"))
    observation = _observation(price=Decimal("999"))
    result = evaluate(rule, observation, _match(matched=False, confidence=0))
    assert result.decision_code == DecisionCode.PRODUCT_MISMATCH


def test_decision_result_has_rule_id() -> None:
    result = evaluate(_rule(id=42), _observation(), _match())
    assert result.rule_id == 42


# --- Phase 25: profitability mode relaxes target_price, never max_price ----


def test_target_price_still_rejects_without_profitability_thresholds() -> None:
    """Backward compatibility: a plain WatchRule (no minimum_net_profit/
    minimum_roi_pct configured — every real WatchRule before Phase 25)
    keeps the exact old behavior."""
    rule = _rule(target_price=Decimal("56"))
    observation = _observation(price=Decimal("60"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is False
    assert result.decision_code == DecisionCode.TARGET_NOT_REACHED


def test_target_price_does_not_reject_in_profitability_mode() -> None:
    """The exact scenario from the spec: a 56€ target buy price is no
    longer a hard notification gate once minimum_net_profit/
    minimum_roi_pct are configured — a 60€ restock can still alert."""
    rule = _rule(target_price=Decimal("56"), minimum_net_profit=Decimal("20"))
    observation = _observation(price=Decimal("60"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is True


def test_max_price_still_hard_rejects_in_profitability_mode() -> None:
    """max_price (hard_max_total), when explicitly set, stays an absolute
    ceiling even in profitability mode."""
    rule = _rule(max_price=Decimal("59"), minimum_net_profit=Decimal("20"))
    observation = _observation(price=Decimal("60"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is False
    assert result.decision_code == DecisionCode.PRICE_ABOVE_MAX


def test_profitability_mode_via_roi_threshold_alone() -> None:
    rule = _rule(target_price=Decimal("56"), minimum_roi_pct=Decimal("50"))
    observation = _observation(price=Decimal("60"))
    result = evaluate(rule, observation, _match())
    assert result.allowed is True


# --- Phase 25: effective_*() dual-homed (rule-then-Product) fallbacks ------


def test_effective_helpers_fall_back_to_product() -> None:
    from database.models import Product
    from engine.decision import (
        effective_fixed_fee,
        effective_minimum_net_profit,
        effective_minimum_resale_confidence,
        effective_minimum_roi_pct,
        effective_other_costs,
        effective_platform_fee_pct,
        effective_shipping_cost,
        is_profitability_mode,
    )

    product = Product(
        id=1,
        name="X",
        platform_fee_pct=Decimal("9"),
        fixed_fee=Decimal("1"),
        shipping_cost=Decimal("6.5"),
        other_costs=Decimal("2"),
        minimum_net_profit=Decimal("20"),
        minimum_roi_pct=Decimal("30"),
        minimum_resale_confidence="high",
    )
    rule = _rule(product=product)

    assert effective_platform_fee_pct(rule) == Decimal("9")
    assert effective_fixed_fee(rule) == Decimal("1")
    assert effective_shipping_cost(rule) == Decimal("6.5")
    assert effective_other_costs(rule) == Decimal("2")
    assert effective_minimum_net_profit(rule) == Decimal("20")
    assert effective_minimum_roi_pct(rule) == Decimal("30")
    assert effective_minimum_resale_confidence(rule) == "high"
    assert is_profitability_mode(rule) is True


def test_effective_helpers_rule_value_wins_over_product() -> None:
    from database.models import Product
    from engine.decision import effective_minimum_net_profit

    product = Product(id=1, name="X", minimum_net_profit=Decimal("20"))
    rule = _rule(product=product, minimum_net_profit=Decimal("5"))

    assert effective_minimum_net_profit(rule) == Decimal("5")


def test_is_profitability_mode_false_without_any_threshold() -> None:
    from engine.decision import is_profitability_mode

    assert is_profitability_mode(_rule()) is False


def test_effective_resale_price_mode_falls_back_to_product_market() -> None:
    from database.models import Product
    from engine.decision import effective_resale_price_mode

    product = Product(id=1, name="X", resale_price_mode="market")
    rule = _rule(product=product)  # rule itself defaults to "manual"

    assert effective_resale_price_mode(rule) == "market"


def test_effective_estimated_resale_trusted_true_from_product() -> None:
    from database.models import Product
    from engine.decision import effective_estimated_resale_trusted

    product = Product(id=1, name="X", estimated_resale_trusted=True)
    rule = _rule(product=product)

    assert effective_estimated_resale_trusted(rule) is True
