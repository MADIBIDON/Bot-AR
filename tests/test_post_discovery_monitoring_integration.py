"""Phase 38 integration: once discovery auto-links one of the 3 Pokémon
30e Cultura campaign products, DIRECT monitoring of that new WatchRule
must immediately ramp up (burst -> drop-window -> normal, per
engine/post_discovery_cadence.py) — scoped ONLY to a WatchRule whose
Product is drop-window-flagged (Product.scheduled_release_at set); every
other product's WatchRule must be provably unaffected.

created_at is real and server-set at insert time (never faked here) —
elapsed time since linking is simulated by passing a LATER `now` into
is_due(), not by rewriting created_at.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from database import crud
from database.time_utils import ensure_utc
from engine.worker import is_due


def _seed_rule(
    session: Session,
    *,
    ean: str = "0196214144835",
    scheduled_release_at: datetime | None = None,
    check_interval: int = 60,
) -> int:
    product = crud.create_product(
        session, "Pokemon Coffret Dresseur d'Elite 30eme Anniversaire", ean=ean
    )
    if scheduled_release_at is not None:
        crud.update_product(session, product.id, scheduled_release_at=scheduled_release_at)
    merchant = crud.create_merchant(session, "Cultura")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://www.cultura.com/p-pokemon-etb-30eme-anniversaire-12345678.html",
        external_id="p-pokemon-etb-30eme-anniversaire-12345678.html",
    )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=check_interval,
        max_quantity=1,
    )
    return rule.id


def test_freshly_linked_drop_window_rule_checks_at_burst_cadence(session: Session) -> None:
    rule_id = _seed_rule(session, scheduled_release_at=datetime.now(UTC))
    rule = crud.get_watch_rule(session, rule_id)
    linked_at = ensure_utc(rule.created_at)

    now = linked_at + timedelta(seconds=5)
    # 15s since last observation: NOT due at the normal 60s cadence, but
    # IS due at the burst ~10s cadence — proves the burst stage actually
    # applies right after linking.
    last_observed = now - timedelta(seconds=15)

    assert is_due(rule, last_observed, now) is True


def test_same_rule_settles_to_drop_window_cadence_after_the_burst(session: Session) -> None:
    rule_id = _seed_rule(session, scheduled_release_at=datetime.now(UTC))
    rule = crud.get_watch_rule(session, rule_id)
    linked_at = ensure_utc(rule.created_at)

    now = linked_at + timedelta(seconds=400)  # past the 300s burst window

    # 40s since last observation: not due at 60s normal (comfortably
    # under, ignoring jitter), but IS due at the ~30s drop-window cadence
    # (comfortably over even with jitter, at most +3s for a 30s base).
    assert is_due(rule, now - timedelta(seconds=40), now) is True
    # 5s since last observation: NOT due even at the faster drop-window
    # cadence — proves it settled to ~30s, not still at the ~10s burst.
    assert is_due(rule, now - timedelta(seconds=5), now) is False


def test_same_rule_returns_to_its_own_normal_cadence_eventually(session: Session) -> None:
    rule_id = _seed_rule(session, scheduled_release_at=datetime.now(UTC), check_interval=90)
    rule = crud.get_watch_rule(session, rule_id)
    linked_at = ensure_utc(rule.created_at)

    now = linked_at + timedelta(seconds=300 + 1800 + 60)  # well past both windows

    # 40s since last observation: not due at the fast stages, and not
    # due at the rule's own 90s configured interval either.
    assert is_due(rule, now - timedelta(seconds=40), now) is False
    # 120s since last observation: comfortably past the 90s configured
    # interval even with the small per-rule jitter is_due adds (at most
    # 10% of the interval, so <=9s here) — confirms it genuinely returned
    # to its own configured base rate, not stuck at a faster stage.
    assert is_due(rule, now - timedelta(seconds=120), now) is True


def test_unrelated_product_watch_rule_is_completely_unaffected(session: Session) -> None:
    """No Product.scheduled_release_at set — must behave exactly as
    before this phase, regardless of how recently the rule was created."""
    rule_id = _seed_rule(session, ean="1111111111111", scheduled_release_at=None)
    rule = crud.get_watch_rule(session, rule_id)
    now = datetime.now(UTC)

    # Freshly created, 15s since last observation: NOT due at the normal
    # 60s cadence (would only be "due" if the burst cadence wrongly
    # applied to a product that was never drop-window-flagged).
    last_observed = now - timedelta(seconds=15)
    assert is_due(rule, last_observed, now) is False
