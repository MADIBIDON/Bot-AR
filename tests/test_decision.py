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
