"""Post-discovery monitoring cadence — Phase 38.

Distinct from engine/release_awareness.py's scheduled-release ramp (time
UNTIL a future, source-published release, decreasing to zero): this is
time SINCE a WatchRule's own creation (i.e. since discovery just found
and auto-linked it), decreasing FROM a burst rate down to normal. Both
share the same floor discipline; this one adds one explicit, narrow,
real-tested exception below it — see BURST_CHECK_INTERVAL_SECONDS.

Scoped, not global: engine/worker.py only consults this for a WatchRule
whose Product has Product.scheduled_release_at set (Phase 36's "this
product is in an active drop window" marker, today true for exactly the
3 Pokémon 30e Cultura campaign products and nothing else) — every other
WatchRule's cadence is completely unaffected, unchanged passthrough.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from engine.release_awareness import MIN_CHECK_INTERVAL_SECONDS

# Phase 38: a deliberate, narrow exception to the project-wide 30s
# monitoring floor (engine/release_awareness.MIN_CHECK_INTERVAL_SECONDS)
# — never changes that constant itself, and is only ever consulted for a
# WatchRule whose Product is explicitly drop-window-flagged.
#
# Real-tested this session: 6 honest GETs against a live Cultura product
# page (the same GenericSchemaOrgConnector path used for real monitoring)
# at 5s spacing — all HTTP 200, ~150-215ms each, no Retry-After, no
# slowdown. 10s (not the tested-but-only-briefly 5s) is the floor
# actually adopted here: roughly half the request volume of the
# theoretical minimum over the full 5-minute burst window (a ~30x sample
# at 5s spacing was never actually run), while still 6x faster than the
# normal 60s cadence. If Cultura ever answers with a 429, the connector-
# level Retry-After handling (connectors/schema_org.py +
# engine/backoff.py) takes over immediately regardless of this value —
# this floor is a target cadence, never a guarantee against backing off.
BURST_CHECK_INTERVAL_SECONDS = 10

# Requested range was 15-30s for the intermediate "drop window" stage.
# Only the burst stage above was actually real-tested (6 requests at 5s
# spacing, ~30s total) — nothing was tested at a 15-20s cadence
# sustained over the full 30-minute drop_window_seconds this stage
# covers, so this deliberately lands on 30s, the TOP (safest, slowest)
# edge of the requested range and exactly the project-wide floor,
# instead of guessing at the faster edge without real evidence for a
# sustained window that long.
DROP_WINDOW_CHECK_INTERVAL_SECONDS = MIN_CHECK_INTERVAL_SECONDS


@dataclass(frozen=True, slots=True)
class PostDiscoveryFrequencyConfig:
    burst_window_seconds: int = 300  # first 5 minutes after the WatchRule was created
    drop_window_seconds: int = 1800  # next 30 minutes at the intermediate cadence
    burst_check_interval: int = BURST_CHECK_INTERVAL_SECONDS
    drop_check_interval: int = DROP_WINDOW_CHECK_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if self.burst_window_seconds <= 0 or self.drop_window_seconds <= 0:
            raise ValueError("burst_window_seconds and drop_window_seconds must be positive")
        if self.burst_check_interval < 1:
            raise ValueError("burst_check_interval must be positive")
        if self.drop_check_interval < MIN_CHECK_INTERVAL_SECONDS:
            raise ValueError(
                f"drop_check_interval={self.drop_check_interval} would be below the "
                f"{MIN_CHECK_INTERVAL_SECONDS}s floor used everywhere else in this project — "
                "only the burst stage above is a deliberate, narrow, real-tested exception."
            )


def post_discovery_check_interval(
    linked_at: datetime,
    now: datetime,
    base_interval: int,
    config: PostDiscoveryFrequencyConfig | None = None,
) -> int:
    """base_interval (the WatchRule's own configured check_interval) is
    used unchanged once both windows have elapsed — this only ever speeds
    checks up right after a fresh auto-link, never slows them down below
    what the rule was already configured for."""
    config = config or PostDiscoveryFrequencyConfig()
    elapsed_seconds = max(0.0, (now - linked_at).total_seconds())
    if elapsed_seconds <= config.burst_window_seconds:
        return config.burst_check_interval
    if elapsed_seconds <= config.burst_window_seconds + config.drop_window_seconds:
        return config.drop_check_interval
    return base_interval
