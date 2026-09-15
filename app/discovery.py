"""Multi-merchant product discovery orchestration — the app-layer glue
between discovery/ (search + matching) and database/crud.py (persisting
auto-linked Listings/WatchRules). Same reasoning as app/notify.py and
app/resale.py: this is the one place allowed to depend on both.

Called from two places: scripts/watch.py (`add-product`, `discover`) for
an immediate, user-visible run, and app/worker.py for the slow, periodic
DISCOVERY PATH — never from the monitoring fast path (see
app/worker.py's module docstring).

A discovery run never invents a Listing/WatchRule for anything below the
auto_link confidence bar (see discovery/matcher.py) — a plausible but
unconfirmed candidate is reported, never linked. Idempotent: re-running
discovery for the same product is always safe — an already-linked
Listing/WatchRule is detected and left alone, never duplicated.

`run_discovery_for_product` is `async` so app/worker.py can fire it as a
background asyncio task (same shape as purchase/engine.py's
attempt_purchase): each merchant's own (potentially slow) network search
is offloaded to a worker thread via `asyncio.to_thread`, so it never
blocks the event loop the monitoring fast path shares. The matching and
DB-write logic around it stays synchronous on the caller's session —
same argument purchase/engine.py already relies on: asyncio is single-
threaded, so those synchronous stretches between awaits are never
touched concurrently, only interleaved with other coroutines' own
synchronous stretches, which is safe for a plain (non-async) SQLAlchemy
Session even though it is not thread-safe.

Phase 23: a product's merchants are independent of each other (Kairyu
being slow says nothing about RelicTCG or Fuji Store), so their searches
now run concurrently too, bounded by MAX_CONCURRENT_DISCOVERY_MERCHANTS
— collected first (network only, no Session touched), then replayed
through _link_or_report sequentially on the calling session, same
"concurrent network, sequential Session" split as engine/worker.py's
monitoring tick. One merchant's timeout/error is caught inside
_search_one_merchant and turned into a normal (non-"ok") outcome, never
raised into the gather(), so it can never take down or delay the others.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from database import crud
from database.time_utils import ensure_utc
from discovery.base import DiscoveryError, DiscoveryUnavailableError
from discovery.matcher import classify_candidate
from engine.release_awareness import dynamic_check_interval

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.notify import EmbedSender
    from connectors.base import ConnectorProduct
    from database.models import Listing, Merchant, Product
    from discovery.base import RetailDiscoverySource
    from discovery.registry import DiscoveryRegistry

logger = logging.getLogger(__name__)

# Phase 23: renamed from DEFAULT_DISCOVERY_CHECK_INTERVAL_SECONDS, which
# this value never actually was — it's the check_interval given to a
# newly auto-linked WatchRule (i.e. how often the *monitoring* fast path
# re-checks it), confusingly named after the unrelated, much slower
# per-Product discovery_interval (Product.discovery_interval, still 1800s
# by default — see database/models.py). Lowered 300 -> 60 (Priority 8):
# a bot whose whole point is fast drop detection shouldn't default new
# listings to a 5-minute blind spot.
DEFAULT_AUTO_LINKED_CHECK_INTERVAL_SECONDS = 60

DEFAULT_MAX_CONCURRENT_DISCOVERY_MERCHANTS = 3


def max_concurrent_discovery_merchants() -> int:
    """MAX_CONCURRENT_DISCOVERY_MERCHANTS env override, else the default."""
    raw = os.environ.get("MAX_CONCURRENT_DISCOVERY_MERCHANTS", "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENT_DISCOVERY_MERCHANTS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_DISCOVERY_MERCHANTS
    return value if value > 0 else DEFAULT_MAX_CONCURRENT_DISCOVERY_MERCHANTS


@dataclass(frozen=True, slots=True)
class DiscoveryCandidateResult:
    merchant: str
    candidate_name: str
    candidate_url: str
    verdict: str  # "auto_link" | "candidate" | "no_match"
    confidence: int
    reason: str
    listing_id: int | None = None
    watch_rule_id: int | None = None
    already_monitored: bool = False


@dataclass(frozen=True, slots=True)
class MerchantDiscoveryOutcome:
    merchant: str
    status: str  # "ok" | "unavailable" | "error"
    detail: str | None
    candidates: list[DiscoveryCandidateResult] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DiscoveryRunResult:
    product_id: int
    merchants: list[MerchantDiscoveryOutcome]


@dataclass(frozen=True, slots=True)
class _RawMerchantResult:
    merchant_name: str
    status: str  # "ok" | "unavailable" | "error"
    detail: str | None
    candidates: tuple[ConnectorProduct, ...] = ()


async def _search_one_merchant(
    merchant_name: str,
    source: RetailDiscoverySource,
    product: Product,
    semaphore: asyncio.Semaphore,
) -> _RawMerchantResult:
    """Runs entirely off the Session — network only. Every failure mode
    is caught and turned into a normal result rather than raised, so
    asyncio.gather() never has to abort the other merchants over one bad
    one (a timeout, a 429, a bug in a source's search())."""
    async with semaphore:
        try:
            candidates = await asyncio.to_thread(
                source.search, product.name, ean=product.ean, mpn=product.mpn
            )
        except DiscoveryUnavailableError as exc:
            logger.info("discovery merchant=%s unavailable: %s", merchant_name, exc)
            return _RawMerchantResult(merchant_name, "unavailable", str(exc))
        except DiscoveryError as exc:
            logger.warning("discovery merchant=%s failed: %s", merchant_name, exc)
            return _RawMerchantResult(merchant_name, "error", str(exc))
        except Exception as exc:  # noqa: BLE001 - one merchant's bug must never stop the others
            logger.exception("discovery merchant=%s crashed", merchant_name)
            return _RawMerchantResult(merchant_name, "error", str(exc))
        return _RawMerchantResult(merchant_name, "ok", None, tuple(candidates))


async def run_discovery_for_product(
    session: Session,
    product: Product,
    discovery_registry: DiscoveryRegistry,
    *,
    check_interval: int = DEFAULT_AUTO_LINKED_CHECK_INTERVAL_SECONDS,
    now: datetime | None = None,
    max_concurrent: int | None = None,
    notifier: EmbedSender | None = None,
) -> DiscoveryRunResult:
    """notifier (Phase 36): when given, fires exactly one Discord embed
    per genuinely NEW auto-linked Listing (never for an already-monitored
    one, never for a mere CANDIDATE) — see format_discovery_embed's own
    docstring. Defaults to None so every existing caller/test is
    unaffected; app/worker.py's real background call passes the real
    notifier. A Discord failure here is caught and logged, never allowed
    to fail the discovery run itself (same posture as
    purchase/engine.py::_notify_purchase)."""
    now = now or datetime.now(UTC)
    max_concurrent = max_concurrent or max_concurrent_discovery_merchants()
    semaphore = asyncio.Semaphore(max_concurrent)

    merchant_names = discovery_registry.names()
    raw_results = await asyncio.gather(
        *(
            _search_one_merchant(name, discovery_registry.get(name), product, semaphore)
            for name in merchant_names
        )
    )

    outcomes: list[MerchantDiscoveryOutcome] = []
    for raw in raw_results:
        if raw.status != "ok":
            outcomes.append(MerchantDiscoveryOutcome(raw.merchant_name, raw.status, raw.detail))
            continue
        candidate_results = []
        for candidate in raw.candidates:
            result = _link_or_report(session, product, raw.merchant_name, candidate, check_interval)
            candidate_results.append(result)
            is_new_link = (
                result.verdict == "auto_link"
                and not result.already_monitored
                and result.watch_rule_id is not None
            )
            if is_new_link and notifier is not None:
                await _notify_new_listing(notifier, product, raw.merchant_name, candidate)
        outcomes.append(MerchantDiscoveryOutcome(raw.merchant_name, "ok", None, candidate_results))

    product.last_discovery_at = now
    session.commit()
    return DiscoveryRunResult(product_id=product.id, merchants=outcomes)


async def _notify_new_listing(
    notifier: EmbedSender, product: Product, merchant_name: str, candidate: ConnectorProduct
) -> None:
    from notifications.discord.formatter import format_discovery_embed

    embed = format_discovery_embed(
        product_name=product.name,
        merchant=merchant_name,
        url=candidate.url,
        price=candidate.price,
        currency=candidate.currency,
        ean=candidate.ean,
    )
    try:
        await notifier.send_embed(embed)
    except Exception:  # noqa: BLE001 - a Discord failure must never break discovery
        logger.exception(
            "discovery notification failed product=%s merchant=%s", product.id, merchant_name
        )


def _get_or_create_merchant(session: Session, merchant_name: str) -> Merchant:
    """Merchant.name is UNIQUE — a genuine TOCTOU race (this worker's own
    background discovery and a manually-run `discover` CLI process both
    seeing "no merchant yet" for the same name at once) raises
    IntegrityError on the second create, not a silent duplicate. Caught
    here and turned into a plain re-query: the other side's row is simply
    reused, exactly as if this call had seen it in the first place."""
    merchant = crud.get_merchant_by_name(session, merchant_name)
    if merchant is not None:
        return merchant
    try:
        return crud.create_merchant(session, merchant_name)
    except IntegrityError:
        session.rollback()
        merchant = crud.get_merchant_by_name(session, merchant_name)
        if merchant is None:
            raise  # something other than the expected race — surface it
        return merchant


def _get_or_create_listing(
    session: Session, *, product_id: int, merchant_id: int, url: str, external_id: str
) -> Listing:
    """Same race, same fix, for Listing's (merchant_id, external_id)
    unique index — see _get_or_create_merchant()."""
    listing = crud.get_listing_by_merchant_and_external_id(session, merchant_id, external_id)
    if listing is not None:
        return listing
    try:
        return crud.create_listing(
            session,
            product_id=product_id,
            merchant_id=merchant_id,
            url=url,
            external_id=external_id,
        )
    except IntegrityError:
        session.rollback()
        listing = crud.get_listing_by_merchant_and_external_id(session, merchant_id, external_id)
        if listing is None:
            raise
        return listing


def _link_or_report(
    session: Session,
    product: Product,
    merchant_name: str,
    candidate: object,
    check_interval: int,
) -> DiscoveryCandidateResult:
    match = classify_candidate(product, candidate)
    listing_id = watch_rule_id = None
    already_monitored = False

    if match.verdict == "auto_link":
        merchant = _get_or_create_merchant(session, merchant_name)
        listing = _get_or_create_listing(
            session,
            product_id=product.id,
            merchant_id=merchant.id,
            url=candidate.url,
            external_id=candidate.external_id,
        )
        listing_id = listing.id

        existing_rule = crud.get_watch_rule_by_listing_id(session, listing.id)
        if existing_rule is not None:
            already_monitored = True
            watch_rule_id = existing_rule.id
        else:
            rule = crud.create_watch_rule(
                session,
                product_id=product.id,
                listing_id=listing.id,
                check_interval=check_interval,
                max_quantity=product.max_quantity or 1,
            )
            watch_rule_id = rule.id
            logger.info(
                "discovery auto-linked product=%s merchant=%s listing=%s watch_rule=%s",
                product.id,
                merchant_name,
                listing.id,
                rule.id,
            )

    return DiscoveryCandidateResult(
        merchant=merchant_name,
        candidate_name=candidate.name,
        candidate_url=candidate.url,
        verdict=match.verdict,
        confidence=match.confidence,
        reason=match.reason,
        listing_id=listing_id,
        watch_rule_id=watch_rule_id,
        already_monitored=already_monitored,
    )


def effective_discovery_interval(product: Product, now: datetime) -> int:
    """Phase 36: mirrors engine/worker.py::_base_check_interval's exact
    pattern, one layer up — a Product with a real, source-published
    scheduled_release_at ramps its DISCOVERY cadence the same way a
    WatchRule already ramps its monitoring cadence (Phase 31 section 12).
    None (every product before this phase, and any without an announced
    release time) leaves discovery_interval completely unaffected. Public
    (not `_`-prefixed): app/worker.py's scheduler also needs this to
    prioritize which due product to check first — see
    _start_discovery_if_due's own docstring."""
    if product.scheduled_release_at is None:
        return product.discovery_interval
    return dynamic_check_interval(
        ensure_utc(product.scheduled_release_at), now, product.discovery_interval
    )


def is_discovery_due(product: Product, now: datetime) -> bool:
    if product.last_discovery_at is None:
        return True
    last = ensure_utc(product.last_discovery_at)
    return (now - last).total_seconds() >= effective_discovery_interval(product, now)
