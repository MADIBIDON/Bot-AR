"""Phase 36: end-to-end drop-window simulation for the Pokémon 30e /
Cultura campaign.

T0: product absent from Cultura's real (mocked) search response.
T+1 cycle: the exact same product now appears, with a matching EAN.

Expected: exact EAN match, one new WatchRule, no false association, the
new Listing's check_interval already fast (direct monitoring — no
further discovery needed for this reference), and exactly one Discord
notification (never one per merchant search, never one per re-discovery
of an already-linked listing).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.discovery import (
    DEFAULT_AUTO_LINKED_CHECK_INTERVAL_SECONDS,
    effective_discovery_interval,
    is_discovery_due,
    run_discovery_for_product,
)
from connectors.base import ConnectorProduct
from database import crud
from discovery.base import RetailDiscoverySource
from discovery.registry import DiscoveryRegistry
from engine.release_awareness import (
    LIVE_WINDOW_MIN_CHECK_INTERVAL_SECONDS,
    MIN_CHECK_INTERVAL_SECONDS,
)

_REAL_EAN = "0196214144835"  # the real ETB 30e Anniversaire EAN given this session


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


class _ScriptedCulturaSource(RetailDiscoverySource):
    """Returns nothing at T0, then the real product at T+1 — simulating
    Cultura genuinely listing it between two discovery cycles."""

    def __init__(self) -> None:
        self.listed = False
        self.call_count = 0

    def search(self, query, *, ean=None, mpn=None, limit=10):
        self.call_count += 1
        if not self.listed:
            return []
        return [
            ConnectorProduct(
                external_id="p-pokemon-etb-30eme-anniversaire-12345678.html",
                name="Pokémon Coffret Dresseur d'Elite 30ème Anniversaire",
                price=Decimal("59.99"),
                currency="EUR",
                available=True,
                seller="Cultura",
                url="https://www.cultura.com/p-pokemon-etb-30eme-anniversaire-12345678.html",
                ean=_REAL_EAN,
                mpn="12345678",
            )
        ]


def _seed_campaign_product(session: Session) -> int:
    product = crud.create_product(
        session,
        "Pokemon Coffret Dresseur d'Elite 30eme Anniversaire",
        ean=_REAL_EAN,
    )
    return product.id  # discovery_interval defaults to 1800 already


def test_drop_window_cadence_activates_fast_discovery(session: Session) -> None:
    """A product with scheduled_release_at in the past (this campaign's
    "no announced exact time, but urgent now" case) must immediately use
    the fastest safe discovery interval, not the normal 1800s."""
    product_id = _seed_campaign_product(session)
    now = datetime.now(UTC)
    crud.update_product(session, product_id, scheduled_release_at=now - timedelta(minutes=1))
    product = crud.get_product(session, product_id)

    interval = effective_discovery_interval(product, now)

    assert interval == LIVE_WINDOW_MIN_CHECK_INTERVAL_SECONDS
    assert interval < product.discovery_interval  # genuinely faster than normal


def test_product_without_release_at_keeps_normal_cadence(session: Session) -> None:
    product_id = _seed_campaign_product(session)
    product = crud.get_product(session, product_id)
    now = datetime.now(UTC)

    assert effective_discovery_interval(product, now) == 1800


def test_t0_absent_then_t1_appears_exact_match_one_notification(session: Session) -> None:
    product_id = _seed_campaign_product(session)
    product = crud.get_product(session, product_id)
    source = _ScriptedCulturaSource()
    registry = DiscoveryRegistry()
    registry.register("Cultura", source)
    notifier = _FakeNotifier()

    # --- T0: product genuinely absent from Cultura ---
    t0 = datetime.now(UTC)
    result_t0 = asyncio.run(
        run_discovery_for_product(session, product, registry, now=t0, notifier=notifier)
    )

    assert source.call_count == 1
    cultura_outcome = next(m for m in result_t0.merchants if m.merchant == "Cultura")
    assert cultura_outcome.status == "ok"
    assert cultura_outcome.candidates == []
    assert crud.list_watch_rules(session, product_id=product_id) == []
    assert notifier.sent_embeds == []  # nothing to notify about yet

    # --- T+1 cycle: Cultura now lists the real product ---
    source.listed = True
    product = crud.get_product(session, product_id)  # re-fetch, last_discovery_at updated
    t1 = t0 + timedelta(seconds=MIN_CHECK_INTERVAL_SECONDS)
    result_t1 = asyncio.run(
        run_discovery_for_product(session, product, registry, now=t1, notifier=notifier)
    )

    assert source.call_count == 2
    cultura_outcome_t1 = next(m for m in result_t1.merchants if m.merchant == "Cultura")
    assert len(cultura_outcome_t1.candidates) == 1
    candidate = cultura_outcome_t1.candidates[0]
    assert candidate.verdict == "auto_link"
    assert candidate.confidence == 100  # EAN exact match
    assert candidate.already_monitored is False

    rules = crud.list_watch_rules(session, product_id=product_id)
    assert len(rules) == 1
    new_rule = rules[0]
    assert new_rule.listing.merchant.name == "Cultura"
    assert new_rule.listing.external_id == "p-pokemon-etb-30eme-anniversaire-12345678.html"
    # Direct monitoring is already fast — no further discovery needed for
    # THIS reference; check_interval matches the existing auto-linked
    # default, itself already a fast 60s (Priority 8 default).
    assert new_rule.check_interval == DEFAULT_AUTO_LINKED_CHECK_INTERVAL_SECONDS

    assert len(notifier.sent_embeds) == 1
    embed = notifier.sent_embeds[0]
    assert "New Listing" in embed.title
    assert embed.url == candidate.candidate_url

    # --- T+2 cycle: re-running discovery for the same product must never
    # create a duplicate WatchRule or send a second notification (already
    # monitored — discovery for this exact reference is now redundant,
    # matching section 3's "réduire fortement le discovery inutile"). ---
    product = crud.get_product(session, product_id)
    t2 = t1 + timedelta(seconds=MIN_CHECK_INTERVAL_SECONDS)
    result_t2 = asyncio.run(
        run_discovery_for_product(session, product, registry, now=t2, notifier=notifier)
    )

    cultura_outcome_t2 = next(m for m in result_t2.merchants if m.merchant == "Cultura")
    assert cultura_outcome_t2.candidates[0].already_monitored is True
    assert len(crud.list_watch_rules(session, product_id=product_id)) == 1  # still exactly one
    assert len(notifier.sent_embeds) == 1  # still exactly one — no duplicate notification


def test_wrong_ean_candidate_never_auto_links(session: Session) -> None:
    """No false association: a same-family but different-EAN Cultura
    listing must stay a CANDIDATE at most, never auto-link."""
    product_id = _seed_campaign_product(session)
    product = crud.get_product(session, product_id)

    class _WrongEanSource(RetailDiscoverySource):
        def search(self, query, *, ean=None, mpn=None, limit=10):
            return [
                ConnectorProduct(
                    external_id="p-pokemon-ev08-coffret-dresseur-d-elite-10816948.html",
                    name="EV08 coffret Dresseur d'Elite - Pokémon",
                    price=Decimal("55.99"),
                    currency="EUR",
                    available=True,
                    seller="Cultura",
                    url="https://www.cultura.com/p-pokemon-ev08-coffret-dresseur-d-elite-10816948.html",
                    ean="0820650559259",  # a real but DIFFERENT EAN
                    mpn="10816948",
                )
            ]

    registry = DiscoveryRegistry()
    registry.register("Cultura", _WrongEanSource())
    notifier = _FakeNotifier()

    result = asyncio.run(run_discovery_for_product(session, product, registry, notifier=notifier))

    cultura_outcome = next(m for m in result.merchants if m.merchant == "Cultura")
    assert cultura_outcome.candidates[0].verdict == "no_match"
    assert crud.list_watch_rules(session, product_id=product_id) == []
    assert notifier.sent_embeds == []


def test_is_discovery_due_respects_dynamic_interval(session: Session) -> None:
    product_id = _seed_campaign_product(session)
    now = datetime.now(UTC)
    crud.update_product(
        session, product_id, scheduled_release_at=now - timedelta(minutes=1), last_discovery_at=now
    )
    product = crud.get_product(session, product_id)

    # Not due yet (well under the fast 30s floor).
    assert is_discovery_due(product, now + timedelta(seconds=5)) is False
    # Due once the fast interval has actually elapsed.
    assert (
        is_discovery_due(product, now + timedelta(seconds=MIN_CHECK_INTERVAL_SECONDS + 1)) is True
    )
