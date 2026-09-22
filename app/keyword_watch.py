"""Catalogue watch: "tell me about anything matching this keyword".

The product-first pipeline (Product -> discovery -> Listing -> WatchRule)
can only ever see what somebody typed in by hand. On 16-17/09 the
reference monitors announced Pokémon items across a dozen shops this bot
was structurally blind to, for exactly that reason.

This runs the opposite way round: one free-text query per merchant per
cycle, against the merchant's own public search API (the very same
RetailDiscoverySource implementations already used by product
discovery — no new network surface), and reports anything that is new,
or that has come back into stock since the last run.

Two things it deliberately does NOT do:
  * it never creates Products, Listings or WatchRules on its own — a
    find is an alert, and promoting it to a monitored product stays a
    human decision (this project's standing "prefer NO_MATCH over a
    wrong auto-link" posture);
  * it never buys anything.

One merchant failing (network, rate limit, unavailable search) never
stops the others — same per-item isolation as app/discovery.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from app.relevance import assess, parse_exclude_terms
from database import crud
from discovery.base import DiscoveryError, DiscoveryUnavailableError
from notifications.dedup import get_default_cooldown
from notifications.discord.formatter import format_catalog_find_embed

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from sqlalchemy.orm import Session

    from app.notify import EmbedSender
    from connectors.base import ConnectorProduct
    from database.models import KeywordWatch
    from discovery.registry import DiscoveryRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KeywordFind:
    """Something worth telling a human about."""

    product: ConnectorProduct
    merchant: str
    reason: str  # "new" | "restock"


@dataclass(frozen=True, slots=True)
class KeywordWatchResult:
    keyword: str
    searched_merchants: int
    failed_merchants: tuple[tuple[str, str], ...]
    finds: tuple[KeywordFind, ...]
    suppressed_over_max_price: int
    suppressed_irrelevant: int = 0


def _classify(
    session: Session, watch: KeywordWatch, merchant: str, product: ConnectorProduct
) -> str | None:
    """ "new", "restock", or None when this is not news."""
    seen = crud.get_keyword_watch_seen(
        session, keyword_watch_id=watch.id, merchant=merchant, external_id=product.external_id
    )
    if seen is None:
        return "new"
    if product.available and not seen.available:
        return "restock"
    return None


def run_keyword_watch(
    session: Session,
    watch: KeywordWatch,
    registry: DiscoveryRegistry,
    *,
    now: datetime | None = None,
) -> KeywordWatchResult:
    """Searches every merchant that can search, records what was seen,
    and returns what is worth alerting on. Pure of Discord: the caller
    decides how to deliver (see notify_keyword_finds)."""
    now = now or datetime.now(UTC)
    finds: list[KeywordFind] = []
    failures: list[tuple[str, str]] = []
    suppressed = 0
    irrelevant = 0
    searched = 0
    extra_excludes = parse_exclude_terms(watch.exclude_terms)

    for merchant in registry.names():
        try:
            source = registry.get(merchant)
            results = source.search(watch.keyword, limit=20)
            searched += 1
        except DiscoveryUnavailableError as exc:
            failures.append((merchant, str(exc)))
            continue
        except (DiscoveryError, Exception) as exc:  # noqa: BLE001 - never stop the other merchants
            failures.append((merchant, str(exc)))
            logger.warning("keyword_watch=%s merchant=%s failed: %s", watch.id, merchant, exc)
            continue

        for product in results:
            # A shop's search engine is a recall tool, not a relevance
            # filter (21/09: "pokemon 30 ans" returned a JPN booster, a
            # backpack and promo singles). Irrelevant hits are dropped
            # before being recorded at all — see app/relevance.py.
            verdict = assess(
                product.name,
                keyword=watch.keyword,
                sealed_only=watch.sealed_only,
                extra_exclude_terms=extra_excludes,
            )
            if not verdict.relevant:
                irrelevant += 1
                continue
            reason = _classify(session, watch, merchant, product)
            crud.upsert_keyword_watch_seen(
                session,
                keyword_watch_id=watch.id,
                merchant=merchant,
                external_id=product.external_id,
                name=product.name,
                url=product.url,
                price=product.price,
                available=product.available,
                now=now,
            )
            if reason is None:
                continue
            # A brand-new listing that is already out of stock is not
            # actionable — record it so a later restock IS news, but do
            # not wake anyone up for it.
            if reason == "new" and not product.available:
                continue
            if watch.max_price is not None and product.price > Decimal(watch.max_price):
                suppressed += 1
                continue
            finds.append(KeywordFind(product=product, merchant=merchant, reason=reason))

    crud.touch_keyword_watch(session, watch.id, last_searched_at=now)
    return KeywordWatchResult(
        keyword=watch.keyword,
        searched_merchants=searched,
        failed_merchants=tuple(failures),
        finds=tuple(finds),
        suppressed_over_max_price=suppressed,
        suppressed_irrelevant=irrelevant,
    )


async def notify_keyword_finds(
    result: KeywordWatchResult,
    notifier: EmbedSender,
    *,
    dispatch: Callable[[Coroutine[object, object, None]], None] | None = None,
) -> int:
    """Sends one embed per find. A Discord failure is logged, never
    raised — a lost alert must not take the watch loop down with it."""
    sent = 0
    for find in result.finds:
        # Two watches ("pokemon 30 ans" and "pokemon 30eme anniversaire")
        # routinely find the same listing — observed live 21/09, where
        # every Hikaru item was recorded twice. Keyed on the listing
        # itself, not the watch, so it alerts once whichever watch finds
        # it first; same cooldown and price rule as monitoring alerts.
        if not get_default_cooldown().should_send(
            watch_rule_id=0,
            event_type=f"catalog:{find.reason}:{find.merchant}:{find.product.external_id}",
            price=str(find.product.price),
            now=datetime.now(UTC),
        ):
            continue
        embed = format_catalog_find_embed(
            product=find.product,
            merchant=find.merchant,
            keyword=result.keyword,
            reason=find.reason,
        )
        coro = _send(notifier, embed, keyword=result.keyword)
        if dispatch is not None:
            dispatch(coro)
        else:
            await coro
        sent += 1
    return sent


async def _send(notifier: EmbedSender, embed: object, *, keyword: str) -> None:
    try:
        await notifier.send_embed(embed)
    except Exception as exc:  # noqa: BLE001 - a Discord failure must never crash the watch
        logger.error("keyword=%r catalogue alert not delivered: %s", keyword, exc)
