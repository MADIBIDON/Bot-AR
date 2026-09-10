from __future__ import annotations

from decimal import Decimal

import pytest

from engine.ranking import (
    OpportunityCandidate,
    Priority,
    RankingConfig,
    RankingThresholds,
    rank_opportunities,
)


def _candidate(**overrides: object) -> OpportunityCandidate:
    defaults: dict[str, object] = dict(
        watch_rule_id=1,
        merchant="Kairyu",
        product_name="Duopack Evoli",
        purchase_price=Decimal("74.90"),
        estimated_resale_price=Decimal("110"),
        net_profit=Decimal("25"),
        roi_pct=Decimal("33"),
        net_margin_pct=Decimal("22"),
        resale_confidence="high",
        in_stock=True,
        match_confidence=100,
        market_sample_size=10,
    )
    defaults.update(overrides)
    return OpportunityCandidate(**defaults)  # type: ignore[arg-type]


def test_single_candidate_is_ranked_first() -> None:
    ranked = rank_opportunities([_candidate()])

    assert len(ranked) == 1
    assert ranked[0].rank == 1
    assert not ranked[0].excluded


def test_higher_roi_ranks_above_lower_roi_all_else_equal() -> None:
    good = _candidate(watch_rule_id=1, roi_pct=Decimal("80"))
    bad = _candidate(watch_rule_id=2, roi_pct=Decimal("10"))

    ranked = rank_opportunities([bad, good])

    assert [r.watch_rule_id for r in ranked] == [1, 2]


def test_higher_profit_ranks_above_lower_profit() -> None:
    good = _candidate(watch_rule_id=1, net_profit=Decimal("90"), roi_pct=Decimal("33"))
    bad = _candidate(watch_rule_id=2, net_profit=Decimal("5"), roi_pct=Decimal("33"))

    ranked = rank_opportunities([bad, good])

    assert ranked[0].watch_rule_id == 1


def test_higher_margin_ranks_above_lower_margin() -> None:
    good = _candidate(watch_rule_id=1, net_margin_pct=Decimal("45"))
    bad = _candidate(watch_rule_id=2, net_margin_pct=Decimal("5"))

    ranked = rank_opportunities([bad, good])

    assert ranked[0].watch_rule_id == 1


def test_low_market_confidence_scores_below_high_confidence() -> None:
    good = _candidate(watch_rule_id=1, resale_confidence="high")
    bad = _candidate(watch_rule_id=2, resale_confidence="low")

    ranked = rank_opportunities([bad, good])

    assert ranked[0].watch_rule_id == 1
    assert ranked[0].score > ranked[1].score


def test_low_match_confidence_scores_below_high_match_confidence() -> None:
    good = _candidate(watch_rule_id=1, match_confidence=100)
    bad = _candidate(watch_rule_id=2, match_confidence=20)

    ranked = rank_opportunities([bad, good])

    assert ranked[0].watch_rule_id == 1
    assert ranked[0].score > ranked[1].score


def test_out_of_stock_is_penalized_not_excluded_by_default() -> None:
    in_stock = _candidate(watch_rule_id=1, in_stock=True)
    out_of_stock = _candidate(watch_rule_id=2, in_stock=False)

    ranked = rank_opportunities([out_of_stock, in_stock])

    assert ranked[0].watch_rule_id == 1
    out = next(r for r in ranked if r.watch_rule_id == 2)
    assert not out.excluded
    assert "out_of_stock_penalty_applied" in out.notes
    assert out.score < ranked[0].score


def test_out_of_stock_can_be_excluded_via_config() -> None:
    out_of_stock = _candidate(watch_rule_id=1, in_stock=False)

    ranked = rank_opportunities([out_of_stock], RankingConfig(exclude_out_of_stock=True))

    assert ranked[0].excluded
    assert ranked[0].exclusion_reason == "out of stock"
    assert ranked[0].priority == Priority.IGNORE


def test_negative_profit_caps_score_very_low() -> None:
    losing = _candidate(
        watch_rule_id=1,
        net_profit=Decimal("-20"),
        roi_pct=Decimal("-25"),
        net_margin_pct=Decimal("-30"),
        resale_confidence="high",
        match_confidence=100,
    )

    ranked = rank_opportunities([losing])

    assert ranked[0].score <= RankingConfig().negative_profit_score_cap
    assert "negative_profit_cap_applied" in ranked[0].notes


def test_score_is_bounded_0_to_100() -> None:
    extreme_good = _candidate(
        watch_rule_id=1,
        roi_pct=Decimal("10000"),
        net_profit=Decimal("100000"),
        net_margin_pct=Decimal("99"),
    )
    extreme_bad = _candidate(
        watch_rule_id=2,
        roi_pct=Decimal("-9999"),
        net_profit=Decimal("-100000"),
        net_margin_pct=Decimal("-99"),
        resale_confidence="low",
        match_confidence=0,
        in_stock=False,
    )

    for candidate in (extreme_good, extreme_bad):
        ranked = rank_opportunities([candidate])
        assert Decimal("0") <= ranked[0].score <= Decimal("100")


def test_candidate_without_resale_estimate_is_excluded_not_scored() -> None:
    candidate = _candidate(
        estimated_resale_price=None, net_profit=None, roi_pct=None, net_margin_pct=None
    )

    ranked = rank_opportunities([candidate])

    assert ranked[0].excluded
    assert ranked[0].exclusion_reason == "no resale estimate available"
    assert ranked[0].score == Decimal("0")
    assert ranked[0].priority == Priority.IGNORE


def test_ordering_is_stable_and_deterministic_across_runs() -> None:
    candidates = [
        _candidate(watch_rule_id=3, roi_pct=Decimal("50")),
        _candidate(watch_rule_id=1, roi_pct=Decimal("80")),
        _candidate(watch_rule_id=2, roi_pct=Decimal("50")),
    ]

    first_run = [r.watch_rule_id for r in rank_opportunities(list(candidates))]
    second_run = [r.watch_rule_id for r in rank_opportunities(list(reversed(candidates)))]

    assert first_run == second_run


def test_exact_ties_break_on_watch_rule_id_ascending() -> None:
    a = _candidate(watch_rule_id=5)
    b = _candidate(watch_rule_id=2)

    ranked = rank_opportunities([a, b])

    assert [r.watch_rule_id for r in ranked] == [2, 5]


def test_ranking_config_rejects_negative_weight() -> None:
    with pytest.raises(ValueError, match="negative"):
        RankingConfig(roi_weight=Decimal("-1"))


def test_ranking_config_rejects_all_zero_weights() -> None:
    with pytest.raises(ValueError, match="at least one weight"):
        RankingConfig(
            roi_weight=Decimal("0"),
            profit_weight=Decimal("0"),
            margin_weight=Decimal("0"),
            resale_confidence_weight=Decimal("0"),
            match_confidence_weight=Decimal("0"),
            sample_size_weight=Decimal("0"),
        )


def test_ranking_thresholds_reject_out_of_order_values() -> None:
    with pytest.raises(ValueError, match="thresholds must satisfy"):
        RankingThresholds(top_min=Decimal("50"), high_min=Decimal("60"))


def test_custom_weights_change_ranking_outcome() -> None:
    high_roi_low_confidence = _candidate(
        watch_rule_id=1, roi_pct=Decimal("90"), resale_confidence="low"
    )
    low_roi_high_confidence = _candidate(
        watch_rule_id=2, roi_pct=Decimal("5"), resale_confidence="high"
    )

    roi_heavy = RankingConfig(
        roi_weight=Decimal("90"),
        profit_weight=Decimal("2"),
        margin_weight=Decimal("2"),
        resale_confidence_weight=Decimal("2"),
        match_confidence_weight=Decimal("2"),
        sample_size_weight=Decimal("2"),
    )
    confidence_heavy = RankingConfig(
        roi_weight=Decimal("2"),
        profit_weight=Decimal("2"),
        margin_weight=Decimal("2"),
        resale_confidence_weight=Decimal("90"),
        match_confidence_weight=Decimal("2"),
        sample_size_weight=Decimal("2"),
    )

    roi_ranked = rank_opportunities([high_roi_low_confidence, low_roi_high_confidence], roi_heavy)
    confidence_ranked = rank_opportunities(
        [high_roi_low_confidence, low_roi_high_confidence], confidence_heavy
    )

    assert roi_ranked[0].watch_rule_id == 1
    assert confidence_ranked[0].watch_rule_id == 2


def test_custom_priority_thresholds_change_classification() -> None:
    candidate = _candidate()
    lenient = RankingThresholds(
        top_min=Decimal("1"),
        high_min=Decimal("0"),
        medium_min=Decimal("0"),
        low_min=Decimal("0"),
    )

    ranked = rank_opportunities([candidate], thresholds=lenient)

    assert ranked[0].priority == Priority.TOP


def test_missing_market_sample_size_scores_zero_for_that_dimension() -> None:
    candidate = _candidate(market_sample_size=None)

    ranked = rank_opportunities([candidate])

    assert ranked[0].score_breakdown["sample_size"] == Decimal("0")


def test_score_breakdown_contains_expected_keys() -> None:
    ranked = rank_opportunities([_candidate()])

    breakdown = ranked[0].score_breakdown
    for key in ("roi", "profit", "margin", "resale_confidence", "match_confidence"):
        assert key in breakdown


def test_empty_candidate_list_returns_empty_ranking() -> None:
    assert rank_opportunities([]) == []
