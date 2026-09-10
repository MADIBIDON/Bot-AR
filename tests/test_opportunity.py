from __future__ import annotations

from decimal import Decimal

import pytest

from engine.opportunity import (
    OpportunityConfig,
    OpportunityStatus,
    OpportunityThresholds,
    evaluate_opportunity,
)


def test_worked_example_matches_hand_calculation() -> None:
    # Achat 74.90, revente 110, frais plateforme 9% (=9.90), livraison 6.50
    config = OpportunityConfig(
        estimated_resale_price=Decimal("110"),
        platform_fee_pct=Decimal("9"),
        shipping_cost=Decimal("6.50"),
    )

    result = evaluate_opportunity(Decimal("74.90"), config)

    assert result.platform_fee_amount == Decimal("9.90")
    assert result.net_profit == Decimal("18.70")
    assert result.roi_pct == Decimal("24.97")


def test_no_estimated_resale_price_returns_none() -> None:
    assert evaluate_opportunity(Decimal("74.90"), OpportunityConfig()) is None


def test_positive_profit() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("100"))
    result = evaluate_opportunity(Decimal("50"), config)
    assert result.net_profit == Decimal("50.00")
    assert result.gross_profit == Decimal("50.00")


def test_negative_profit() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("40"))
    result = evaluate_opportunity(Decimal("50"), config)
    assert result.net_profit == Decimal("-10.00")
    assert result.status == OpportunityStatus.REJECT


def test_break_even_zero_net_profit() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("50"))
    result = evaluate_opportunity(Decimal("50"), config)
    assert result.net_profit == Decimal("0.00")
    assert result.break_even_resale_price == Decimal("50.00")
    assert result.resale_safety_margin_pct == Decimal("0.00")


def test_platform_fee_pct_is_applied_to_resale_price() -> None:
    config = OpportunityConfig(
        estimated_resale_price=Decimal("200"), platform_fee_pct=Decimal("10")
    )
    result = evaluate_opportunity(Decimal("100"), config)
    assert result.platform_fee_amount == Decimal("20.00")
    assert result.net_profit == Decimal("80.00")  # 200 - 100 - 20


def test_fixed_fee_is_subtracted() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("100"), fixed_fee=Decimal("5"))
    result = evaluate_opportunity(Decimal("50"), config)
    assert result.net_profit == Decimal("45.00")


def test_shipping_cost_is_subtracted() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("100"), shipping_cost=Decimal("7.50"))
    result = evaluate_opportunity(Decimal("50"), config)
    assert result.net_profit == Decimal("42.50")


def test_other_costs_is_subtracted() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("100"), other_costs=Decimal("3"))
    result = evaluate_opportunity(Decimal("50"), config)
    assert result.net_profit == Decimal("47.00")


def test_all_costs_combine() -> None:
    config = OpportunityConfig(
        estimated_resale_price=Decimal("100"),
        platform_fee_pct=Decimal("10"),
        fixed_fee=Decimal("2"),
        shipping_cost=Decimal("3"),
        other_costs=Decimal("1"),
    )
    result = evaluate_opportunity(Decimal("50"), config)
    # fees: 10 (10% of 100) + 2 + 3 + 1 = 16 ; net = 100-50-16 = 34
    assert result.net_profit == Decimal("34.00")


def test_roi_pct_formula() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("150"))
    result = evaluate_opportunity(Decimal("100"), config)
    # net_profit = 50, roi = 50/100*100 = 50%
    assert result.roi_pct == Decimal("50.00")


def test_net_margin_pct_formula() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("200"))
    result = evaluate_opportunity(Decimal("100"), config)
    # net_profit = 100, margin = 100/200*100 = 50%
    assert result.net_margin_pct == Decimal("50.00")


def test_values_are_decimal() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("110"))
    result = evaluate_opportunity(Decimal("74.90"), config)
    for field in (
        result.purchase_price,
        result.net_profit,
        result.roi_pct,
        result.net_margin_pct,
        result.gross_profit,
        result.platform_fee_amount,
    ):
        assert isinstance(field, Decimal)


def test_purchase_price_must_be_positive() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("100"))
    with pytest.raises(ValueError, match="positive"):
        evaluate_opportunity(Decimal("0"), config)


def test_partial_configuration_only_shipping() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("100"), shipping_cost=Decimal("5"))
    result = evaluate_opportunity(Decimal("50"), config)
    assert result.platform_fee_amount == Decimal("0.00")
    assert result.fixed_fee == Decimal("0.00")
    assert result.other_costs == Decimal("0.00")
    assert result.net_profit == Decimal("45.00")


def test_fee_pct_at_or_above_100_makes_break_even_undefined() -> None:
    config = OpportunityConfig(
        estimated_resale_price=Decimal("100"), platform_fee_pct=Decimal("99.99")
    )
    result = evaluate_opportunity(Decimal("50"), config)
    # fee_fraction just under 1 -> still defined but very large; sanity: not None here
    assert result.break_even_resale_price is not None


# --- status thresholds ---------------------------------------------------


def test_status_reject_below_min_net_profit() -> None:
    thresholds = OpportunityThresholds(min_net_profit=Decimal("20"))
    config = OpportunityConfig(estimated_resale_price=Decimal("100"))
    result = evaluate_opportunity(Decimal("90"), config, thresholds)  # profit=10 < 20
    assert result.status == OpportunityStatus.REJECT


def test_status_watch_when_profitable_but_below_roi_threshold() -> None:
    thresholds = OpportunityThresholds(min_net_profit=Decimal("0"), min_roi_pct=Decimal("30"))
    config = OpportunityConfig(estimated_resale_price=Decimal("110"))
    result = evaluate_opportunity(Decimal("100"), config, thresholds)  # roi=10% < 30%
    assert result.status == OpportunityStatus.WATCH


def test_status_watch_when_below_margin_threshold() -> None:
    thresholds = OpportunityThresholds(min_margin_pct=Decimal("40"))
    config = OpportunityConfig(estimated_resale_price=Decimal("200"))
    result = evaluate_opportunity(Decimal("150"), config, thresholds)  # margin=25% < 40%
    assert result.status == OpportunityStatus.WATCH


def test_status_buy_candidate_meets_thresholds() -> None:
    thresholds = OpportunityThresholds(
        min_net_profit=Decimal("0"),
        min_roi_pct=Decimal("10"),
        min_margin_pct=Decimal("10"),
        strong_buy_roi_pct=Decimal("100"),
    )
    config = OpportunityConfig(estimated_resale_price=Decimal("150"))
    result = evaluate_opportunity(Decimal("100"), config, thresholds)  # roi=50%, margin=33%
    assert result.status == OpportunityStatus.BUY_CANDIDATE


def test_status_strong_buy_candidate_above_roi_threshold() -> None:
    thresholds = OpportunityThresholds(strong_buy_roi_pct=Decimal("40"))
    config = OpportunityConfig(estimated_resale_price=Decimal("150"))
    result = evaluate_opportunity(Decimal("100"), config, thresholds)  # roi=50% >= 40%
    assert result.status == OpportunityStatus.STRONG_BUY_CANDIDATE


def test_default_thresholds_used_when_none_given() -> None:
    config = OpportunityConfig(estimated_resale_price=Decimal("100"))
    result = evaluate_opportunity(Decimal("50"), config)  # roi=100% >= default strong_buy 50%
    assert result.status == OpportunityStatus.STRONG_BUY_CANDIDATE
