"""One place for the single UTC-discipline rule this project needs:
SQLite does not preserve tzinfo across a round trip (every column this
project writes a timestamp to stores a naive value), while every
datetime this project ever *constructs* is UTC by construction
(products/observation.py's ProductObservation guarantees it, and every
other timestamp derives from datetime.now(UTC)). So a naive value read
back from the database is always safely re-attached to UTC — never
guessed, never a different timezone.

Before this module existed, four call sites (engine/worker.py,
app/discovery.py, app/opportunity_snapshot.py, purchase/engine.py) each
hand-wrote the same two-line "if naive, attach UTC" check. Centralized
here (Phase 26 audit) so the rule is stated once and every comparison
against a freshly-read timestamp uses it the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime


def ensure_utc(value: datetime) -> datetime:
    """Returns `value` unchanged if it already carries a timezone,
    otherwise the same wall-clock time with UTC attached."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
