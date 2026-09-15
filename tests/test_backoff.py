from __future__ import annotations

from datetime import UTC, datetime, timedelta

from engine.backoff import BackoffTracker, is_retryable_error


def test_429_is_retryable() -> None:
    assert is_retryable_error("rate limited (429) fetching https://x") is True


def test_timeout_is_retryable() -> None:
    assert is_retryable_error("timeout fetching https://x") is True


def test_network_error_is_retryable() -> None:
    assert is_retryable_error("network error fetching https://x: boom") is True


def test_5xx_is_retryable() -> None:
    assert is_retryable_error("HTTP 503 fetching https://x") is True
    assert is_retryable_error("HTTP 500 fetching https://x") is True


def test_404_is_not_retryable() -> None:
    assert is_retryable_error("product page not found: https://x") is False


def test_403_is_not_retryable() -> None:
    assert is_retryable_error("access forbidden (403) fetching https://x") is False


def test_disabled_rule_message_is_not_retryable() -> None:
    assert is_retryable_error("watch rule 1 is disabled") is False


def test_no_failure_means_not_blocked() -> None:
    tracker = BackoffTracker()
    assert tracker.is_blocked(1, datetime.now(UTC)) is False


def test_retryable_failure_blocks_until_delay_elapses() -> None:
    tracker = BackoffTracker(base_seconds=30, max_seconds=3600)
    now = datetime.now(UTC)

    tracker.record_failure(1, "timeout fetching x", now)

    assert tracker.is_blocked(1, now + timedelta(seconds=1)) is True
    assert tracker.is_blocked(1, now + timedelta(seconds=31)) is False


def test_non_retryable_failure_never_blocks() -> None:
    tracker = BackoffTracker(base_seconds=30, max_seconds=3600)
    now = datetime.now(UTC)

    tracker.record_failure(1, "product page not found: x", now)

    assert tracker.is_blocked(1, now + timedelta(seconds=1)) is False


def test_backoff_grows_exponentially() -> None:
    tracker = BackoffTracker(base_seconds=10, max_seconds=10000)
    now = datetime.now(UTC)

    tracker.record_failure(1, "timeout", now)
    assert tracker.current_delay_seconds(1) == 10

    tracker.record_failure(1, "timeout", now)
    assert tracker.current_delay_seconds(1) == 20

    tracker.record_failure(1, "timeout", now)
    assert tracker.current_delay_seconds(1) == 40


def test_backoff_is_capped_at_max() -> None:
    tracker = BackoffTracker(base_seconds=1000, max_seconds=1500)
    now = datetime.now(UTC)

    for _ in range(5):
        tracker.record_failure(1, "timeout", now)

    assert tracker.current_delay_seconds(1) == 1500


def test_success_resets_backoff() -> None:
    tracker = BackoffTracker(base_seconds=30, max_seconds=3600)
    now = datetime.now(UTC)

    tracker.record_failure(1, "timeout", now)
    assert tracker.current_delay_seconds(1) == 30

    tracker.record_success(1)

    assert tracker.current_delay_seconds(1) == 0
    assert tracker.is_blocked(1, now) is False


def test_backoff_is_per_rule() -> None:
    tracker = BackoffTracker(base_seconds=30, max_seconds=3600)
    now = datetime.now(UTC)

    tracker.record_failure(1, "timeout", now)

    assert tracker.is_blocked(1, now + timedelta(seconds=1)) is True
    assert tracker.is_blocked(2, now + timedelta(seconds=1)) is False


# --- Phase 38: real Retry-After respect ---------------------------------


def test_retry_after_longer_than_exponential_delay_wins() -> None:
    """A real Retry-After of 120s must never be shortened to the 30s a
    first-failure exponential backoff would otherwise pick."""
    tracker = BackoffTracker(base_seconds=30, max_seconds=3600)
    now = datetime.now(UTC)

    tracker.record_failure(1, "rate limited (429) fetching https://x (retry_after=120s)", now)

    assert tracker.current_delay_seconds(1) == 120
    assert tracker.is_blocked(1, now + timedelta(seconds=100)) is True
    assert tracker.is_blocked(1, now + timedelta(seconds=121)) is False


def test_retry_after_shorter_than_exponential_delay_does_not_shorten_it() -> None:
    """Retry-After only ever raises the floor, never lowers it below what
    exponential backoff already computed."""
    tracker = BackoffTracker(base_seconds=100, max_seconds=3600)
    now = datetime.now(UTC)

    tracker.record_failure(1, "rate limited (429) fetching https://x (retry_after=5s)", now)

    assert tracker.current_delay_seconds(1) == 100


def test_429_without_retry_after_uses_plain_exponential_backoff() -> None:
    """No marker at all (every 429 before Phase 38, or a merchant that
    never sends the header) — behaves exactly as before."""
    tracker = BackoffTracker(base_seconds=30, max_seconds=3600)
    now = datetime.now(UTC)

    tracker.record_failure(1, "rate limited (429) fetching https://x", now)

    assert tracker.current_delay_seconds(1) == 30
