"""Turns a list of (already product-filtered) MarketObservations into a
single resale price estimate. Pure, deterministic, no I/O — the same
guarantees as engine/decision.py and engine/opportunity.py.

Method: median after simple IQR-based outlier removal (Tukey's fences,
1.5x IQR) — a well-known, deterministic, explainable robust-statistics
method, not a raw mean (one absurd listing must not skew the estimate)
and not machine learning.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from market_data.models import MarketObservation

_OUTLIER_MIN_SAMPLE = 4
_IQR_MULTIPLIER = Decimal("1.5")


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class EstimatorConfig:
    min_sample_for_medium: int = 3
    min_sample_for_high: int = 8
    max_dispersion_pct_for_high: Decimal = Decimal("40")


@dataclass(frozen=True, slots=True)
class ResaleEstimate:
    estimated_price: Decimal | None
    sample_size: int
    min_price: Decimal | None
    max_price: Decimal | None
    median_price: Decimal | None
    mean_price: Decimal | None
    confidence: Confidence
    source: str
    method: str
    based_on_sold_data: bool
    reason: str


def _median(sorted_values: list[Decimal]) -> Decimal:
    n = len(sorted_values)
    mid = n // 2
    if n % 2 == 1:
        return sorted_values[mid]
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2


def _quartile(sorted_values: list[Decimal], q: Decimal) -> Decimal:
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    position = q * (n - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, n - 1)
    fraction = position - lower_index
    return (
        sorted_values[lower_index]
        + (sorted_values[upper_index] - sorted_values[lower_index]) * fraction
    )


def _remove_outliers(sorted_values: list[Decimal]) -> list[Decimal]:
    if len(sorted_values) < _OUTLIER_MIN_SAMPLE:
        return sorted_values
    q1 = _quartile(sorted_values, Decimal("0.25"))
    q3 = _quartile(sorted_values, Decimal("0.75"))
    iqr = q3 - q1
    if iqr == 0:
        return sorted_values
    lower_fence = q1 - _IQR_MULTIPLIER * iqr
    upper_fence = q3 + _IQR_MULTIPLIER * iqr
    filtered = [v for v in sorted_values if lower_fence <= v <= upper_fence]
    return filtered or sorted_values


def _classify_confidence(
    sample_size: int,
    min_price: Decimal,
    max_price: Decimal,
    median_price: Decimal,
    based_on_sold_data: bool,
    config: EstimatorConfig,
) -> Confidence:
    if sample_size < config.min_sample_for_medium:
        return Confidence.LOW
    if not based_on_sold_data:
        # Asking prices, never a confirmed transaction — capped below
        # HIGH regardless of sample size or dispersion, as required.
        return Confidence.MEDIUM
    dispersion_pct = (
        ((max_price - min_price) / median_price) * 100 if median_price > 0 else Decimal("999")
    )
    dispersion_ok = dispersion_pct <= config.max_dispersion_pct_for_high
    if sample_size >= config.min_sample_for_high and dispersion_ok:
        return Confidence.HIGH
    return Confidence.MEDIUM


def estimate_resale_price(
    observations: list[MarketObservation],
    config: EstimatorConfig | None = None,
) -> ResaleEstimate:
    config = config or EstimatorConfig()

    if not observations:
        return ResaleEstimate(
            estimated_price=None,
            sample_size=0,
            min_price=None,
            max_price=None,
            median_price=None,
            mean_price=None,
            confidence=Confidence.LOW,
            source="unknown",
            method="none",
            based_on_sold_data=False,
            reason="No market observations available.",
        )

    source = observations[0].source
    based_on_sold_data = all(obs.sold is True for obs in observations)

    prices = sorted(obs.price for obs in observations)
    filtered = _remove_outliers(prices)
    method = "median" if filtered == prices else "median_after_outlier_removal"

    median_price = _median(filtered)
    mean_price = sum(filtered) / len(filtered)
    min_price = filtered[0]
    max_price = filtered[-1]
    sample_size = len(filtered)

    confidence = _classify_confidence(
        sample_size, min_price, max_price, median_price, based_on_sold_data, config
    )

    quality = "sold prices" if based_on_sold_data else "asking prices (not confirmed sales)"
    reason = (
        f"Estimated from {sample_size} {quality} via {source} "
        f"(dropped {len(prices) - sample_size} outlier(s))."
    )

    return ResaleEstimate(
        estimated_price=median_price,
        sample_size=sample_size,
        min_price=min_price,
        max_price=max_price,
        median_price=median_price,
        mean_price=mean_price,
        confidence=confidence,
        source=source,
        method=method,
        based_on_sold_data=based_on_sold_data,
        reason=reason,
    )
