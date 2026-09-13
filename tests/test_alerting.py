"""engine/alerting.py — Phase 31's AlertTier mapping and reason codes.
Pure, no I/O, same style as tests/test_ranking.py (candidates built by
hand, no database)."""

from __future__ import annotations

from decimal import Decimal

from engine.alerting import (
    FAST_SELL_THROUGH,
    GOOD_MARKET_SAMPLE,
    GOOD_NET_PROFIT,
    HIGH_CONFIDENCE,
    HIGH_MARGIN,
    HIGH_ROI,
    LOW_CONFIDENCE,
    LOW_MATCH_CONFIDENCE,
    NEEDS_MARKET_DATA,
    NEGATIVE_NET_PROFIT,
    OUT_OF_STOCK,
    AlertTier,
    alert_tier_for,
    build_opportunity_intelligence,
    compute_reason_codes,
)
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
        resale_price_source="manual",
    )
    defaults.update(overrides)
    return OpportunityCandidate(**defaults)  # type: ignore[arg-type]


def _rank_one(candidate, config=None, thresholds=None):
    config = config or RankingConfig()
    return rank_opportunities([candidate], config, thresholds)[0]


def test_alert_tier_ignore_for_low_score() -> None:
    candidate = _candidate(
        roi_pct=Decimal("0"),
        net_profit=Decimal("0"),
        net_margin_pct=Decimal("0"),
        resale_confidence="low",
        match_confidence=0,
        market_sample_size=None,
    )
    ranked = _rank_one(candidate)
    assert ranked.priority == Priority.IGNORE
    assert alert_tier_for(ranked) == AlertTier.IGNORE


def test_alert_tier_watch_for_low_and_medium_priority() -> None:
    thresholds = RankingThresholds(top_min=Decimal("95"), high_min=Decimal("90"))
    candidate = _candidate()
    ranked = _rank_one(candidate, thresholds=thresholds)
    assert ranked.priority in (Priority.LOW, Priority.MEDIUM)
    assert alert_tier_for(ranked) == AlertTier.WATCH


def test_alert_tier_high_for_priority_high() -> None:
    candidate = _candidate(
        roi_pct=Decimal("60"), net_profit=Decimal("60"), net_margin_pct=Decimal("30")
    )
    ranked = _rank_one(candidate)
    assert ranked.priority == Priority.HIGH
    assert alert_tier_for(ranked) == AlertTier.HIGH


def test_alert_tier_urgent_for_priority_top() -> None:
    candidate = _candidate(
        roi_pct=Decimal("150"),
        net_profit=Decimal("150"),
        net_margin_pct=Decimal("50"),
        market_sample_size=10,
    )
    ranked = _rank_one(candidate)
    assert ranked.priority == Priority.TOP
    assert alert_tier_for(ranked) == AlertTier.URGENT


def test_alert_tier_needs_market_data_when_no_resale_estimate() -> None:
    candidate = _candidate(
        estimated_resale_price=None, net_profit=None, roi_pct=None, net_margin_pct=None
    )
    ranked = _rank_one(candidate)
    assert ranked.excluded
    assert alert_tier_for(ranked) == AlertTier.NEEDS_MARKET_DATA
    assert compute_reason_codes(candidate, ranked, RankingConfig()) == (NEEDS_MARKET_DATA,)


def test_alert_tier_ignore_when_excluded_for_out_of_stock() -> None:
    candidate = _candidate(in_stock=False)
    ranked = _rank_one(candidate, config=RankingConfig(exclude_out_of_stock=True))
    assert ranked.excluded
    assert alert_tier_for(ranked) == AlertTier.IGNORE
    assert compute_reason_codes(candidate, ranked, RankingConfig()) == (OUT_OF_STOCK,)


def test_reason_codes_flag_negative_net_profit() -> None:
    candidate = _candidate(net_profit=Decimal("-5"), roi_pct=Decimal("-10"))
    ranked = _rank_one(candidate)
    codes = compute_reason_codes(candidate, ranked, RankingConfig())
    assert NEGATIVE_NET_PROFIT in codes


def test_reason_codes_flag_low_match_confidence() -> None:
    candidate = _candidate(match_confidence=40)
    ranked = _rank_one(candidate)
    codes = compute_reason_codes(candidate, ranked, RankingConfig())
    assert LOW_MATCH_CONFIDENCE in codes


def test_reason_codes_high_roi_and_good_profit_use_config_caps_not_hardcoded_numbers() -> None:
    config = RankingConfig(
        roi_normalization_cap_pct=Decimal("20"), profit_normalization_cap=Decimal("10")
    )
    candidate = _candidate(roi_pct=Decimal("25"), net_profit=Decimal("15"))
    ranked = _rank_one(candidate, config=config)
    codes = compute_reason_codes(candidate, ranked, config)
    assert HIGH_ROI in codes
    assert GOOD_NET_PROFIT in codes
    # The exact same candidate against the *default* (much higher) caps
    # must not trip the same codes — proves the thresholds are genuinely
    # read from config, not a copy-pasted constant.
    default_ranked = _rank_one(candidate)
    default_codes = compute_reason_codes(candidate, default_ranked, RankingConfig())
    assert HIGH_ROI not in default_codes
    assert GOOD_NET_PROFIT not in default_codes


def test_reason_codes_high_margin_uses_config_cap() -> None:
    config = RankingConfig(margin_normalization_cap_pct=Decimal("10"))
    candidate = _candidate(net_margin_pct=Decimal("15"))
    ranked = _rank_one(candidate, config=config)
    assert HIGH_MARGIN in compute_reason_codes(candidate, ranked, config)


def test_reason_codes_confidence_flags() -> None:
    high = _candidate(resale_confidence="high")
    low = _candidate(resale_confidence="low")
    ranked_high = _rank_one(high)
    ranked_low = _rank_one(low)
    assert HIGH_CONFIDENCE in compute_reason_codes(high, ranked_high, RankingConfig())
    assert LOW_CONFIDENCE in compute_reason_codes(low, ranked_low, RankingConfig())


def test_reason_codes_good_market_sample() -> None:
    candidate = _candidate(market_sample_size=8)
    ranked = _rank_one(candidate)
    assert GOOD_MARKET_SAMPLE in compute_reason_codes(candidate, ranked, RankingConfig())


def test_fast_sell_through_is_never_emitted_no_real_data_source() -> None:
    """Phase 31 section 6: no integrated market source reports sales
    velocity (eBay Browse API is active-listings only) — this constant
    must exist for future use but never actually appear, since fabricating
    it would violate "never invent missing data"."""
    candidate = _candidate()
    ranked = _rank_one(candidate)
    assert FAST_SELL_THROUGH not in compute_reason_codes(candidate, ranked, RankingConfig())


def test_build_opportunity_intelligence_composes_everything() -> None:
    candidate = _candidate()
    config = RankingConfig()
    ranked = _rank_one(candidate, config=config)
    intelligence = build_opportunity_intelligence(candidate, ranked, config)

    assert intelligence.alert_tier == alert_tier_for(ranked)
    assert intelligence.score == ranked.score
    assert intelligence.net_profit == candidate.net_profit
    assert intelligence.roi_pct == candidate.roi_pct
    assert intelligence.resale_price_source == "manual"
    assert intelligence.reason_codes == compute_reason_codes(candidate, ranked, config)
