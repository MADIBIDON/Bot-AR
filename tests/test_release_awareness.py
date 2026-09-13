from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.release_awareness import (
    MIN_CHECK_INTERVAL_SECONDS,
    ReleaseFrequencyConfig,
    ReleaseState,
    classify_release_state,
    dynamic_check_interval,
)

RELEASE_AT = datetime(2026, 9, 15, 7, 0, 0, tzinfo=UTC)


def test_classify_upcoming_well_before_release() -> None:
    now = RELEASE_AT - timedelta(hours=2)
    state = classify_release_state(RELEASE_AT, now, available=False, was_ever_available=False)
    assert state == ReleaseState.UPCOMING


def test_classify_pre_release_window_at_t_minus_20_min() -> None:
    now = RELEASE_AT - timedelta(minutes=20)
    state = classify_release_state(RELEASE_AT, now, available=False, was_ever_available=False)
    assert state == ReleaseState.PRE_RELEASE_WINDOW


def test_classify_live_once_release_time_passed() -> None:
    now = RELEASE_AT + timedelta(seconds=1)
    state = classify_release_state(RELEASE_AT, now, available=False, was_ever_available=False)
    assert state == ReleaseState.LIVE


def test_classify_sold_out_after_being_available_post_release() -> None:
    now = RELEASE_AT + timedelta(hours=1)
    state = classify_release_state(RELEASE_AT, now, available=False, was_ever_available=True)
    assert state == ReleaseState.SOLD_OUT


def test_classify_still_live_while_actually_available() -> None:
    now = RELEASE_AT + timedelta(minutes=5)
    state = classify_release_state(RELEASE_AT, now, available=True, was_ever_available=True)
    assert state == ReleaseState.LIVE


def test_dynamic_interval_normal_well_before_release() -> None:
    now = RELEASE_AT - timedelta(hours=2)
    assert dynamic_check_interval(RELEASE_AT, now, base_interval=300) == 300


def test_dynamic_interval_still_normal_at_t_minus_30_min() -> None:
    now = RELEASE_AT - timedelta(minutes=30)
    assert dynamic_check_interval(RELEASE_AT, now, base_interval=300) == 300


def test_dynamic_interval_faster_at_t_minus_5_min() -> None:
    now = RELEASE_AT - timedelta(minutes=5)
    config = ReleaseFrequencyConfig()
    assert dynamic_check_interval(RELEASE_AT, now, base_interval=300) == config.fast_check_interval


def test_dynamic_interval_high_frequency_at_t_minus_60_sec() -> None:
    now = RELEASE_AT - timedelta(seconds=60)
    assert dynamic_check_interval(RELEASE_AT, now, base_interval=300) == MIN_CHECK_INTERVAL_SECONDS


def test_dynamic_interval_fastest_once_live() -> None:
    now = RELEASE_AT + timedelta(seconds=1)
    assert dynamic_check_interval(RELEASE_AT, now, base_interval=300) == MIN_CHECK_INTERVAL_SECONDS


def test_dynamic_interval_never_below_the_safety_floor_even_with_bad_config() -> None:
    with pytest.raises(ValueError):
        ReleaseFrequencyConfig(high_frequency_check_interval=5)


def test_config_rejects_out_of_order_windows() -> None:
    with pytest.raises(ValueError):
        ReleaseFrequencyConfig(pre_release_window_seconds=60, fast_window_seconds=300)
