"""Scheduled-release state and dynamic check-interval, Phase 31 section 12.

Pure, deterministic, no I/O — same guarantees as the rest of engine/.
Only ever consulted for a WatchRule that has a real, source-published
scheduled_release_at (e.g. the Nike SNKRS launch page's own
commerceStartDate — see connectors/nike_launch.py) — every other rule's
check_interval is completely unaffected (engine/worker.py's is_due() only
calls dynamic_check_interval() when scheduled_release_at is not None).

Never invents a release time and never lowers the check interval below
engine.worker.MIN_CHECK_INTERVAL_SECONDS — "fastest safe interval" means
exactly that floor, not an unbounded hammer as a drop approaches. This is
the same "respecter les sites, pas de scraping agressif" rule the rest of
this project already follows (backoff on 429/timeout, jitter, bounded
concurrency); a scheduled release is not an excuse to abandon it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MIN_CHECK_INTERVAL_SECONDS = 30  # kept in sync with engine.worker's own floor

# A separate, deliberately lower floor that applies ONLY while a rule's
# real, known release window is actually live (seconds_to_release <= 0).
# Everything else in this project keeps the 30s floor.
#
# Why a second floor at all: measured 21/09 against the reference
# monitors, we were 8 minutes behind on a real King Jouet restock while
# checking that listing every 52-57s. A restock window can close in
# under two minutes, so a 30s floor means routinely arriving after it is
# over. This band only ever applies to a handful of rules (those with a
# real scheduled_release_at), it only applies during the window itself,
# and every existing protection still holds on top of it: 429 /
# Retry-After is obeyed exactly (engine/backoff.py), failures back off
# exponentially, and each merchant's own rate limit wins over this value.
LIVE_WINDOW_MIN_CHECK_INTERVAL_SECONDS = 8


class ReleaseState(StrEnum):
    UPCOMING = "upcoming"
    PRE_RELEASE_WINDOW = "pre_release_window"
    LIVE = "live"
    SOLD_OUT = "sold_out"


@dataclass(frozen=True, slots=True)
class ReleaseFrequencyConfig:
    """Time-to-release bands, most distant first, and the check_interval
    each one uses. Configurable per the spec ("Exemple configurable") —
    never hardcoded inside classify_release_state/dynamic_check_interval
    themselves."""

    pre_release_window_seconds: int = 1800  # "T-30 min"
    fast_window_seconds: int = 300  # "T-5 min"
    high_frequency_window_seconds: int = 60  # "T-60 sec"

    fast_check_interval: int = 45
    high_frequency_check_interval: int = MIN_CHECK_INTERVAL_SECONDS
    live_check_interval: int = LIVE_WINDOW_MIN_CHECK_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if not (
            self.pre_release_window_seconds
            > self.fast_window_seconds
            > self.high_frequency_window_seconds
            >= 0
        ):
            raise ValueError(
                "windows must satisfy pre_release_window > fast_window > high_frequency_window >= 0"
            )
        for name, interval, floor in (
            ("fast_check_interval", self.fast_check_interval, MIN_CHECK_INTERVAL_SECONDS),
            (
                "high_frequency_check_interval",
                self.high_frequency_check_interval,
                MIN_CHECK_INTERVAL_SECONDS,
            ),
            (
                "live_check_interval",
                self.live_check_interval,
                LIVE_WINDOW_MIN_CHECK_INTERVAL_SECONDS,
            ),
        ):
            if interval < floor:
                raise ValueError(
                    f"{name}={interval} would be below the {floor}s floor "
                    "— never check a real site faster than that."
                )


def classify_release_state(
    scheduled_release_at: datetime,
    now: datetime,
    *,
    available: bool,
    was_ever_available: bool,
    config: ReleaseFrequencyConfig | None = None,
) -> ReleaseState:
    """was_ever_available: whether any *prior* observation of this listing
    ever had available=True — the only way to tell "not live yet" apart
    from "was live, now sold out" when the current observation is False
    either way. Never guessed: the caller (app/opportunity_alerts.py or a
    reporting script) derives it from real ObservationRecord history."""
    config = config or ReleaseFrequencyConfig()
    if now < scheduled_release_at:
        seconds_to_release = (scheduled_release_at - now).total_seconds()
        if seconds_to_release <= config.pre_release_window_seconds:
            return ReleaseState.PRE_RELEASE_WINDOW
        return ReleaseState.UPCOMING
    if not available and was_ever_available:
        return ReleaseState.SOLD_OUT
    return ReleaseState.LIVE


def dynamic_check_interval(
    scheduled_release_at: datetime,
    now: datetime,
    base_interval: int,
    config: ReleaseFrequencyConfig | None = None,
) -> int:
    """base_interval is used unchanged outside every configured window —
    a scheduled release only ever speeds checks up, never slows them down
    below what the rule was already configured for."""
    config = config or ReleaseFrequencyConfig()
    seconds_to_release = (scheduled_release_at - now).total_seconds()

    if seconds_to_release <= 0:
        return max(config.live_check_interval, LIVE_WINDOW_MIN_CHECK_INTERVAL_SECONDS)
    if seconds_to_release <= config.high_frequency_window_seconds:
        return max(config.high_frequency_check_interval, MIN_CHECK_INTERVAL_SECONDS)
    if seconds_to_release <= config.fast_window_seconds:
        return max(config.fast_check_interval, MIN_CHECK_INTERVAL_SECONDS)
    if seconds_to_release <= config.pre_release_window_seconds:
        return base_interval
    return base_interval
