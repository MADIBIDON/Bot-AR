"""engine/post_discovery_cadence.py — Phase 38."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.post_discovery_cadence import (
    BURST_CHECK_INTERVAL_SECONDS,
    DROP_WINDOW_CHECK_INTERVAL_SECONDS,
    PostDiscoveryFrequencyConfig,
    post_discovery_check_interval,
)
from engine.release_awareness import MIN_CHECK_INTERVAL_SECONDS


def test_burst_stage_right_after_creation() -> None:
    linked_at = datetime.now(UTC)
    now = linked_at + timedelta(seconds=1)

    assert post_discovery_check_interval(linked_at, now, 60) == BURST_CHECK_INTERVAL_SECONDS


def test_burst_stage_at_the_edge_of_the_window() -> None:
    linked_at = datetime.now(UTC)
    now = linked_at + timedelta(seconds=300)  # exactly the default burst window

    assert post_discovery_check_interval(linked_at, now, 60) == BURST_CHECK_INTERVAL_SECONDS


def test_drop_window_stage_after_burst_expires() -> None:
    linked_at = datetime.now(UTC)
    now = linked_at + timedelta(seconds=301)

    assert post_discovery_check_interval(linked_at, now, 60) == DROP_WINDOW_CHECK_INTERVAL_SECONDS


def test_normal_stage_once_both_windows_elapsed() -> None:
    linked_at = datetime.now(UTC)
    now = linked_at + timedelta(seconds=300 + 1800 + 1)

    assert post_discovery_check_interval(linked_at, now, 60) == 60


def test_never_slower_than_base_interval_even_if_base_is_unusual() -> None:
    linked_at = datetime.now(UTC)
    now = linked_at + timedelta(hours=2)

    assert post_discovery_check_interval(linked_at, now, 120) == 120


def test_negative_elapsed_is_clamped_to_burst_stage() -> None:
    """now before linked_at (clock skew) must never crash or produce a
    negative/nonsensical interval — treated as "just linked"."""
    linked_at = datetime.now(UTC)
    now = linked_at - timedelta(seconds=5)

    assert post_discovery_check_interval(linked_at, now, 60) == BURST_CHECK_INTERVAL_SECONDS


def test_config_rejects_a_drop_interval_below_the_project_wide_floor() -> None:
    with pytest.raises(ValueError, match="floor"):
        PostDiscoveryFrequencyConfig(drop_check_interval=MIN_CHECK_INTERVAL_SECONDS - 1)


def test_config_allows_the_deliberate_burst_exception_below_the_floor() -> None:
    # The burst stage is the one intentional exception — must not raise.
    config = PostDiscoveryFrequencyConfig(burst_check_interval=5)
    assert config.burst_check_interval == 5


def test_custom_config_windows_are_respected() -> None:
    config = PostDiscoveryFrequencyConfig(
        burst_window_seconds=60,
        drop_window_seconds=120,
        burst_check_interval=8,
        drop_check_interval=35,
    )
    linked_at = datetime.now(UTC)

    assert (
        post_discovery_check_interval(linked_at, linked_at + timedelta(seconds=30), 60, config) == 8
    )
    assert (
        post_discovery_check_interval(linked_at, linked_at + timedelta(seconds=90), 60, config)
        == 35
    )
    assert (
        post_discovery_check_interval(linked_at, linked_at + timedelta(seconds=200), 60, config)
        == 60
    )
