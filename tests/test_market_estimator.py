from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from market_data.estimator import Confidence, EstimatorConfig, estimate_resale_price
from market_data.models import MarketObservation


def _obs(
    price: str, *, sold: bool | None = None, source: str = "ebay", currency: str = "EUR"
) -> MarketObservation:
    return MarketObservation(
        source=source,
        product_name="Pokemon ETB Ecarlate Violet FR",
        price=Decimal(price),
        currency=currency,
        listing_url="https://ebay.example/item/1",
        external_id="1",
        observed_at=datetime.now(UTC),
        sold=sold,
    )


def test_empty_sample_returns_low_confidence_none_price() -> None:
    result = estimate_resale_price([])

    assert result.estimated_price is None
    assert result.sample_size == 0
    assert result.confidence == Confidence.LOW
    assert result.method == "none"
    assert result.source == "unknown"


def test_sample_of_one_uses_that_price_as_median() -> None:
    result = estimate_resale_price([_obs("100")])

    assert result.sample_size == 1
    assert result.median_price == Decimal("100")
    assert result.estimated_price == Decimal("100")
    assert result.confidence == Confidence.LOW  # below min_sample_for_medium


def test_multiple_observations_median_is_robust() -> None:
    observations = [_obs("90"), _obs("100"), _obs("110")]

    result = estimate_resale_price(observations)

    assert result.median_price == Decimal("100")
    assert result.sample_size == 3


def test_outlier_is_dropped_via_iqr() -> None:
    # Four normal prices clustered near 100, one wild outlier at 1000.
    observations = [_obs("95"), _obs("98"), _obs("102"), _obs("105"), _obs("1000")]

    result = estimate_resale_price(observations)

    assert result.sample_size == 4  # the 1000 outlier was dropped
    assert result.max_price == Decimal("105")
    assert result.method == "median_after_outlier_removal"


def test_no_outlier_uses_plain_median_method() -> None:
    observations = [_obs("95"), _obs("98"), _obs("102"), _obs("105")]

    result = estimate_resale_price(observations)

    assert result.sample_size == 4
    assert result.method == "median"


def test_confidence_low_below_min_sample_for_medium() -> None:
    result = estimate_resale_price([_obs("100"), _obs("105")])

    assert result.confidence == Confidence.LOW


def test_confidence_medium_when_asking_prices_only() -> None:
    observations = [_obs(str(p), sold=None) for p in (95, 98, 100, 102, 105, 108, 110, 112, 115)]

    result = estimate_resale_price(observations)

    assert result.based_on_sold_data is False
    assert result.confidence == Confidence.MEDIUM  # capped below HIGH: not sold data


def test_confidence_high_requires_sold_data_and_low_dispersion() -> None:
    observations = [_obs(str(p), sold=True) for p in (95, 98, 100, 102, 105, 108, 110, 112, 115)]

    result = estimate_resale_price(observations)

    assert result.based_on_sold_data is True
    assert result.confidence == Confidence.HIGH


def test_confidence_medium_when_sold_but_high_dispersion() -> None:
    observations = [_obs(str(p), sold=True) for p in (50, 60, 70, 200, 210, 220, 230, 240)]

    result = estimate_resale_price(observations)

    assert result.based_on_sold_data is True
    assert result.confidence == Confidence.MEDIUM


def test_custom_estimator_config_thresholds() -> None:
    config = EstimatorConfig(min_sample_for_medium=1)
    result = estimate_resale_price([_obs("100")], config)

    assert result.confidence == Confidence.MEDIUM


# --- Phase 26 audit, section 6: "devise différente" must never be pooled
# silently with the majority currency into one number. ---------------------


def test_minority_currency_observations_are_dropped_not_pooled() -> None:
    """A few USD listings mixed into a mostly-EUR sample (e.g. a
    misconfigured EBAY_MARKETPLACE_ID or a future second market source)
    must never get averaged/medianed together with the EUR ones."""
    observations = [_obs("100", currency="EUR"), _obs("110", currency="EUR")] + [
        _obs("9999", currency="USD")
    ]

    result = estimate_resale_price(observations)

    assert result.sample_size == 2  # only the EUR pair
    assert result.median_price == Decimal("105")
    assert "different currency" in result.reason


def test_single_currency_sample_is_unaffected() -> None:
    observations = [_obs(str(p)) for p in (100, 110, 120)]

    result = estimate_resale_price(observations)

    assert result.sample_size == 3
    assert "different currency" not in result.reason


def test_all_observations_same_minority_split_currency_keeps_the_majority() -> None:
    observations = [_obs("50", currency="USD"), _obs("60", currency="USD")] + [
        _obs("9999", currency="EUR")
    ]

    result = estimate_resale_price(observations)

    assert result.sample_size == 2
    assert result.median_price == Decimal("55")
