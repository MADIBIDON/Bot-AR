"""Manage real WatchRules from the command line — no direct SQLite editing.

Usage:
    python scripts/watch.py add [--url URL] [--merchant NAME] [--name NAME]
                                 [--external-id ID] [--target-price X]
                                 [--max-price X] [--check-interval N]
                                 [--max-quantity N] [--yes]
    python scripts/watch.py edit <id> [--target-price X] [--max-price X]
                                       [--check-interval N] [--max-quantity N]
                                       [--resale-price-mode manual|market]
                                       [--market-source ebay]
    python scripts/watch.py list [--status enabled|disabled|all]
    python scripts/watch.py show <id>
    python scripts/watch.py enable <id>
    python scripts/watch.py disable <id>
    python scripts/watch.py delete <id>
    python scripts/watch.py test <id>
    python scripts/watch.py market <id>
    python scripts/watch.py opportunities [--top N] [--min-priority LOW|MEDIUM|HIGH|TOP]
    python scripts/watch.py purchases [--limit N]
    python scripts/watch.py purchase-status
    python scripts/watch.py purchase-test <listing_id>
    python scripts/watch.py status
    python scripts/watch.py add-product --name "..." --max-price 60
                                         [--target-price X] [--max-quantity N]
                                         [--monitoring-interval S]
                                         [--ean ...] [--gtin ...] [--mpn ...]
    python scripts/watch.py products
    python scripts/watch.py product <id>
    python scripts/watch.py discover <id>
    python scripts/watch.py edit-product <id> [--target-price X] [--max-price X]
                                               [--minimum-net-profit X]
                                               [--minimum-roi-pct X]
                                               [--minimum-resale-confidence low|medium|high]
                                               [--estimated-resale-price X]
                                               [--resale-trusted true|false]
                                               [--platform-fee-pct X] [--fixed-fee X]
                                               [--shipping-cost X] [--other-costs X]
                                               [--resale-price-mode manual|market]
                                               [--market-source ebay]
    python scripts/watch.py enable-product <id>
    python scripts/watch.py disable-product <id>

`add-product` is the primary, product-first workflow (Phase 22): one
product + one max_total_price, searched and monitored across every
supported merchant automatically — see app/discovery.py. The
merchant-by-merchant `add` command above still exists for a single known
listing on a single merchant.

`add --url` identifies the merchant/connector automatically from the
URL's hostname (see connectors/defaults.py for the supported list) and
fetches name/price/currency/stock/external_id/GTIN/MPN, so you don't have
to type in what can be detected. An unsupported hostname, or a detection
failure on a supported one (network error, page changed), falls back to
asking for everything manually. Either way you confirm before anything is
written, unless --yes is passed.

Reusing an existing Listing when one already exists for the same
(merchant, external_id) is the only deduplication this does — no
purchase logic anywhere in this file.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import pidfile
from app.discovery import DEFAULT_AUTO_LINKED_CHECK_INTERVAL_SECONDS, run_discovery_for_product
from app.notify import notify_events_if_allowed
from app.opportunity_snapshot import build_opportunity_candidate
from app.resale import resolve_resale_estimate
from connectors.base import ConnectorError, ConnectorProduct, ProductNotFoundError
from connectors.defaults import (
    build_default_registry,
    domains_for_merchant,
    find_merchant_for_domain,
    supported_domains_summary,
)
from database import crud
from database.models import WatchRule
from database.session import create_all, get_engine, get_session_factory
from database.time_utils import ensure_utc
from discovery.defaults import build_default_discovery_registry
from engine.monitoring import run_check_and_store
from engine.opportunity import OpportunityConfig, evaluate_opportunity
from engine.ranking import Priority, RankingConfig, RankingThresholds, rank_opportunities
from engine.worker import MIN_CHECK_INTERVAL_SECONDS
from market_data.cache import TTLCache
from market_data.defaults import SUPPORTED_MARKET_SOURCES, build_default_market_registry
from market_data.ebay import MissingEbayConfigError
from notifications.discord.client import DiscordNotifier
from notifications.discord.config import load_discord_config
from purchase.config import load_purchase_policy
from purchase.engine import build_decision_context, build_purchase_intent, evaluate_purchase_intent

PID_FILE = Path("data") / "worker.pid"

_market_cache = TTLCache()


def _build_registry():
    """Thin alias kept for tests to monkeypatch a per-test registry
    without touching connectors/defaults.py."""
    return build_default_registry()


def _build_market_registry():
    """Thin alias kept for tests to monkeypatch, same reasoning as
    _build_registry(). Callers must handle MissingEbayConfigError."""
    return build_default_market_registry()


def _build_discovery_registry():
    """Thin alias kept for tests to monkeypatch, same reasoning as
    _build_registry()."""
    return build_default_discovery_registry()


def _get_session() -> Session:
    load_dotenv(override=True)
    engine = get_engine()
    create_all(engine)
    return get_session_factory(engine)()


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def _prompt_decimal(label: str, current: Decimal | None) -> Decimal | None:
    default = str(current) if current is not None else ""
    raw = input(f"{label} (vide pour aucun) [{default}]: ").strip() or default
    if not raw:
        return None
    return _parse_positive_decimal(label, raw)


def _parse_positive_decimal(label: str, raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{label}: {raw!r} is not a valid number") from None
    if value <= 0:
        raise ValueError(f"{label} must be strictly positive, got {value}")
    return value


def _parse_positive_int(label: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{label}: {raw!r} is not a valid integer") from None
    if value <= 0:
        raise ValueError(f"{label} must be strictly positive, got {value}")
    return value


def _parse_check_interval(raw: str) -> int:
    value = _parse_positive_int("check_interval", raw)
    if value < MIN_CHECK_INTERVAL_SECONDS:
        raise ValueError(
            f"check_interval must be at least {MIN_CHECK_INTERVAL_SECONDS}s, got {value}"
        )
    return value


def _validate_price_order(target_price: Decimal | None, max_price: Decimal | None) -> None:
    if target_price is not None and max_price is not None and max_price < target_price:
        raise ValueError(f"max_price ({max_price}) must be >= target_price ({target_price})")


def _parse_non_negative_decimal(label: str, raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{label}: {raw!r} is not a valid number") from None
    if value < 0:
        raise ValueError(f"{label} must not be negative, got {value}")
    return value


def _parse_fee_pct(raw: str) -> Decimal:
    value = _parse_non_negative_decimal("platform_fee_pct", raw)
    if value >= 100:
        raise ValueError(f"platform_fee_pct must be less than 100, got {value}")
    return value


def _parse_market_source(raw: str) -> str:
    if raw not in SUPPORTED_MARKET_SOURCES:
        raise ValueError(
            f"market_source must be one of {', '.join(SUPPORTED_MARKET_SOURCES)}, got {raw!r}"
        )
    return raw


_RESALE_CONFIDENCE_LEVELS = ("low", "medium", "high")


def _parse_resale_confidence(raw: str) -> str:
    lowered = raw.strip().lower()
    if lowered not in _RESALE_CONFIDENCE_LEVELS:
        raise ValueError(
            f"minimum_resale_confidence must be one of {', '.join(_RESALE_CONFIDENCE_LEVELS)}, "
            f"got {raw!r}"
        )
    return lowered


def _parse_bool(label: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ValueError(f"{label} must be true/false, got {raw!r}")


@dataclass
class DetectedProduct:
    merchant_name: str
    connector_name: str | None
    shop_domain: str
    handle: str
    connector_product: ConnectorProduct | None


def _detect_from_url(url: str, merchant_override: str | None) -> DetectedProduct:
    parsed = urlparse(url)
    hostname = parsed.netloc
    handle = parsed.path.rstrip("/").rsplit("/", 1)[-1]

    merchant_def = find_merchant_for_domain(hostname)
    if merchant_def is None:
        print(f"Unsupported domain: {hostname!r}")
        print("Supported merchants:")
        print(supported_domains_summary())
        return DetectedProduct(
            merchant_name=merchant_override or hostname,
            connector_name=None,
            shop_domain=hostname,
            handle=handle,
            connector_product=None,
        )

    merchant_name = merchant_override or merchant_def.name
    connector = merchant_def.build_connector()
    try:
        product = connector.get_product(handle)
    except (ConnectorError, ProductNotFoundError) as exc:
        print(f"Auto-detection failed ({exc}); please enter the product details manually.")
        product = None
    return DetectedProduct(
        merchant_name=merchant_name,
        connector_name=type(connector).__name__,
        shop_domain=hostname,
        handle=handle,
        connector_product=product,
    )


def cmd_add(args: argparse.Namespace) -> int:
    session = _get_session()

    url = args.url or _prompt("Product URL")
    if not url:
        print("A product URL is required.")
        return 1

    detected = _detect_from_url(url, args.merchant)
    product_data = detected.connector_product

    merchant_name = args.merchant or detected.merchant_name
    print(f"URL detected: {url}")
    print(f"Merchant: {merchant_name}")
    print(f"Connector: {detected.connector_name or 'none (unsupported domain)'}")
    if product_data is not None:
        print(f"Product: {product_data.name}")
        print(f"Price: {product_data.price} {product_data.currency}")
        print(f"Stock: {'in stock' if product_data.available else 'out of stock'}")
        print(f"external_id: {product_data.external_id}")
        print(f"ean/gtin:    {product_data.ean}")
        print(f"mpn:         {product_data.mpn}")

    name = args.name or (product_data.name if product_data else None) or _prompt("Product name")
    external_id = args.external_id or (
        product_data.external_id if product_data else detected.handle
    )
    ean = product_data.ean if product_data else None
    mpn = product_data.mpn if product_data else None
    canonical_url = product_data.url if product_data else url

    try:
        if args.target_price:
            target_price = _parse_positive_decimal("target_price", args.target_price)
        elif args.yes:
            target_price = None
        else:
            target_price = _prompt_decimal("Target price", None)

        if args.max_price:
            max_price = _parse_positive_decimal("max_price", args.max_price)
        elif args.yes:
            max_price = None
        else:
            max_price = _prompt_decimal("Max price", None)

        check_interval_raw = args.check_interval or (
            "300" if args.yes else _prompt("Check interval (seconds)", "300")
        )
        check_interval = _parse_check_interval(check_interval_raw)

        max_quantity_raw = args.max_quantity or ("1" if args.yes else _prompt("Max quantity", "1"))
        max_quantity = _parse_positive_int("max_quantity", max_quantity_raw)

        _validate_price_order(target_price, max_price)
    except ValueError as exc:
        print(f"Invalid value: {exc}")
        return 1

    print("\nAbout to create:")
    print(f"  merchant:       {merchant_name}")
    print(f"  product:        {name}")
    print(f"  listing url:    {canonical_url}")
    print(f"  external_id:    {external_id}")
    print(f"  target_price:   {target_price}")
    print(f"  max_price:      {max_price}")
    print(f"  check_interval: {check_interval}s")
    print(f"  max_quantity:   {max_quantity}")

    if not args.yes and input("\nCreate this WatchRule? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return 1

    merchant = crud.get_merchant_by_name(session, merchant_name)
    if merchant is None:
        merchant = crud.create_merchant(session, merchant_name)

    existing_listing = crud.get_listing_by_merchant_and_external_id(
        session, merchant.id, external_id
    )
    if existing_listing is not None:
        print(f"Reusing existing listing={existing_listing.id} for this merchant/external_id.")
        listing = existing_listing
        product = listing.product
    else:
        product = crud.create_product(session, name, ean=ean, mpn=mpn)
        listing = crud.create_listing(
            session,
            product_id=product.id,
            merchant_id=merchant.id,
            url=canonical_url,
            external_id=external_id,
        )

    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=check_interval,
        max_quantity=max_quantity,
        target_price=target_price,
        max_price=max_price,
    )

    print(f"\nCreated watch_rule={rule.id} (product={product.id}, listing={listing.id}).")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    session = _get_session()
    enabled = {"enabled": True, "disabled": False, "all": None}[args.status]
    rules = crud.list_watch_rules(session, enabled=enabled)
    if not rules:
        print("No watch rules.")
        return 0
    for rule in rules:
        merchant_name = rule.listing.merchant.name if rule.listing else "-"
        status = "enabled" if rule.enabled else "disabled"
        print(
            f"[{rule.id}] {status:8} {rule.product.name!r} via {merchant_name} "
            f"target={rule.target_price} max={rule.max_price} interval={rule.check_interval}s"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    session = _get_session()
    rule = crud.get_watch_rule(session, args.id)
    if rule is None:
        print(f"No watch_rule={args.id}")
        return 1
    print(f"watch_rule:     {rule.id}")
    print(f"enabled:        {rule.enabled}")
    print(f"product:        {rule.product.name} (id={rule.product.id})")
    print(f"ean/mpn:        {rule.product.ean} / {rule.product.mpn}")
    if rule.listing:
        print(f"merchant:       {rule.listing.merchant.name}")
        print(f"listing url:    {rule.listing.url}")
        print(f"external_id:    {rule.listing.external_id}")
    else:
        print("listing:        none (global rule)")
    print(f"target_price:   {rule.target_price}")
    print(f"max_price:      {rule.max_price}")
    print(f"check_interval: {rule.check_interval}s")
    print(f"max_quantity:   {rule.max_quantity}")
    print(f"resale mode:    {rule.resale_price_mode}")
    if rule.resale_price_mode == "market":
        print(f"market source:  {rule.market_source}")
    else:
        print(f"resale price:   {rule.estimated_resale_price}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    session = _get_session()
    rule = crud.enable_watch_rule(session, args.id)
    if rule is None:
        print(f"No watch_rule={args.id}")
        return 1
    print(f"watch_rule={rule.id} enabled.")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    session = _get_session()
    rule = crud.disable_watch_rule(session, args.id)
    if rule is None:
        print(f"No watch_rule={args.id}")
        return 1
    print(f"watch_rule={rule.id} disabled.")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    session = _get_session()
    rule = crud.get_watch_rule(session, args.id)
    if rule is None:
        print(f"No watch_rule={args.id}")
        return 1

    fields: dict[str, object] = {}
    try:
        if args.target_price is not None:
            fields["target_price"] = _parse_positive_decimal("target_price", args.target_price)
        if args.max_price is not None:
            fields["max_price"] = _parse_positive_decimal("max_price", args.max_price)
        if args.check_interval is not None:
            fields["check_interval"] = _parse_check_interval(args.check_interval)
        if args.max_quantity is not None:
            fields["max_quantity"] = _parse_positive_int("max_quantity", args.max_quantity)
        if args.estimated_resale_price is not None:
            fields["estimated_resale_price"] = _parse_positive_decimal(
                "estimated_resale_price", args.estimated_resale_price
            )
        if args.platform_fee_pct is not None:
            fields["platform_fee_pct"] = _parse_fee_pct(args.platform_fee_pct)
        if args.fixed_fee is not None:
            fields["fixed_fee"] = _parse_non_negative_decimal("fixed_fee", args.fixed_fee)
        if args.shipping_cost is not None:
            fields["shipping_cost"] = _parse_non_negative_decimal(
                "shipping_cost", args.shipping_cost
            )
        if args.other_costs is not None:
            fields["other_costs"] = _parse_non_negative_decimal("other_costs", args.other_costs)
        if args.resale_price_mode is not None:
            fields["resale_price_mode"] = args.resale_price_mode
        if args.market_source is not None:
            fields["market_source"] = _parse_market_source(args.market_source)

        effective_target = fields.get("target_price", rule.target_price)
        effective_max = fields.get("max_price", rule.max_price)
        _validate_price_order(effective_target, effective_max)

        effective_mode = fields.get("resale_price_mode", rule.resale_price_mode)
        effective_source = fields.get("market_source", rule.market_source)
        if effective_mode == "market" and effective_source is None:
            raise ValueError("resale_price_mode=market requires --market-source")
    except ValueError as exc:
        print(f"Invalid value: {exc}")
        return 1

    if not fields:
        print(
            "Nothing to update — pass at least one of --target-price/--max-price/"
            "--check-interval/--max-quantity/--estimated-resale-price/--platform-fee-pct/"
            "--fixed-fee/--shipping-cost/--other-costs/--resale-price-mode/--market-source."
        )
        return 1

    updated = crud.update_watch_rule(session, args.id, **fields)
    changes = ", ".join(f"{key}={value}" for key, value in fields.items())
    print(f"watch_rule={updated.id} updated: {changes}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    session = _get_session()
    try:
        deleted = crud.delete_watch_rule(session, args.id)
    except IntegrityError:
        session.rollback()
        print("Cannot delete watch rule because historical events exist.")
        print(f"Use `disable {args.id}` to stop monitoring while preserving history.")
        return 1
    if not deleted:
        print(f"No watch_rule={args.id}")
        return 1
    print(f"watch_rule={args.id} deleted.")
    return 0


async def _run_test(session: Session, rule: WatchRule) -> int:
    registry = _build_registry()
    result = run_check_and_store(session, rule, registry)

    if not result.success:
        print(f"check failed: {result.error}")
        return 1

    obs = result.observation
    print(f"product:  {obs.name}")
    print(f"price:    {obs.price} {obs.currency}")
    print(f"in stock: {obs.available}")
    print(
        f"match:    method={result.match_result.method} "
        f"confidence={result.match_result.confidence} matched={result.match_result.matched}"
    )

    market_registry = None
    if rule.resale_price_mode == "market":
        try:
            market_registry = _build_market_registry()
        except MissingEbayConfigError as exc:
            print(f"market data unavailable: {exc}")

    discord_config = load_discord_config()
    async with DiscordNotifier(discord_config) as notifier:
        decision = await notify_events_if_allowed(
            rule,
            result.observation,
            result.match_result,
            result.events,
            notifier,
            market_registry,
            _market_cache,
            session=session,
        )

    event_names = [e.event_type.value for e in result.events] or ["none"]
    print(f"events:   {', '.join(event_names)}")
    print(
        f"decision: {decision.decision_code.value} (allowed={decision.allowed}) — {decision.reason}"
    )
    notified = decision.allowed and bool(result.events)
    print(f"notified: {notified}")

    resale_price: Decimal | None = rule.estimated_resale_price
    if rule.resale_price_mode == "market":
        print("Market estimate:")
        if market_registry is None:
            print("  source: unavailable (market source not configured)")
            resale_price = None
        else:
            estimate = resolve_resale_estimate(rule, market_registry, _market_cache)
            if estimate is None or estimate.sample_size == 0:
                print("  no comparable market observations found")
                resale_price = None
            else:
                print(f"  source: {estimate.source}")
                print(f"  sample size: {estimate.sample_size}")
                print(f"  estimated resale: {estimate.estimated_price} {obs.currency}")
                print(f"  confidence: {estimate.confidence.value}")
                resale_price = estimate.estimated_price
        print()

    print("Opportunity:")
    if resale_price is None:
        if rule.resale_price_mode == "market":
            print("  not available (market data unavailable)")
        else:
            print("  not configured (no estimated_resale_price on this watch rule)")
    else:
        config = OpportunityConfig(
            estimated_resale_price=resale_price,
            platform_fee_pct=rule.platform_fee_pct or Decimal("0"),
            fixed_fee=rule.fixed_fee or Decimal("0"),
            shipping_cost=rule.shipping_cost or Decimal("0"),
            other_costs=rule.other_costs or Decimal("0"),
        )
        opportunity = evaluate_opportunity(obs.price, config)
        print(f"  purchase price: {obs.price} {obs.currency}")
        print(f"  estimated resale: {opportunity.estimated_resale_price} {obs.currency}")
        print(f"  net profit: {opportunity.net_profit} {obs.currency}")
        print(f"  ROI: {opportunity.roi_pct}%")
        print(f"  margin: {opportunity.net_margin_pct}%")
        print(f"  status: {opportunity.status.value}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    session = _get_session()
    rule = crud.get_watch_rule(session, args.id)
    if rule is None:
        print(f"No watch_rule={args.id}")
        return 1
    return asyncio.run(_run_test(session, rule))


def cmd_market(args: argparse.Namespace) -> int:
    session = _get_session()
    rule = crud.get_watch_rule(session, args.id)
    if rule is None:
        print(f"No watch_rule={args.id}")
        return 1
    if rule.resale_price_mode != "market":
        print(
            f"watch_rule={rule.id} resale_price_mode={rule.resale_price_mode!r}, not 'market'. "
            f"Use `edit {rule.id} --resale-price-mode market --market-source <name>` first."
        )
        return 1

    try:
        market_registry = _build_market_registry()
    except MissingEbayConfigError as exc:
        print(f"market data unavailable: {exc}")
        return 1

    estimate = resolve_resale_estimate(rule, market_registry, _market_cache)
    print(f"Market source: {rule.market_source}")
    if estimate is None:
        print("Matched observations: unavailable (fetch failed — see logs)")
        return 1
    print(f"Matched observations: {estimate.sample_size}")
    if estimate.sample_size == 0:
        print("No comparable observations found.")
        return 0
    print(f"Prices: min={estimate.min_price} max={estimate.max_price} mean={estimate.mean_price}")
    print(f"Median: {estimate.median_price}")
    print(f"Estimated resale: {estimate.estimated_price}")
    print(f"Confidence: {estimate.confidence.value}")
    print(f"Method: {estimate.method}")
    print(f"Reason: {estimate.reason}")
    return 0


_PRIORITY_ORDER = [Priority.IGNORE, Priority.LOW, Priority.MEDIUM, Priority.HIGH, Priority.TOP]


def cmd_opportunities(args: argparse.Namespace) -> int:
    session = _get_session()
    rules = crud.list_watch_rules(session, enabled=True)
    rules_by_id = {rule.id: rule for rule in rules}

    market_registry = None
    if any(rule.resale_price_mode == "market" for rule in rules):
        try:
            market_registry = _build_market_registry()
        except MissingEbayConfigError as exc:
            print(f"market unavailable: {exc}")
            print()

    candidates = []
    skipped_no_data = 0
    for rule in rules:
        candidate = build_opportunity_candidate(session, rule, market_registry, _market_cache)
        if candidate is None:
            skipped_no_data += 1
            continue
        candidates.append(candidate)

    if skipped_no_data:
        print(f"{skipped_no_data} rule(s) skipped: no monitoring data yet.")

    ranked = rank_opportunities(candidates, RankingConfig(), RankingThresholds())

    if args.min_priority is not None:
        min_index = _PRIORITY_ORDER.index(Priority(args.min_priority.lower()))
        ranked = [r for r in ranked if _PRIORITY_ORDER.index(r.priority) >= min_index]

    if args.top is not None:
        ranked = ranked[: args.top]

    if not ranked:
        print("No rankable opportunities.")
        return 0

    header = (
        f"{'ID':<4} | {'Merchant':<14} | {'Product':<28} | {'Buy':>8} | "
        f"{'Resale':>8} | {'Profit':>8} | {'ROI':>7} | {'Score':>6} | Priority"
    )
    print(header)
    for r in ranked:
        resale = str(r.estimated_resale_price) if r.estimated_resale_price is not None else "-"
        profit = str(r.net_profit) if r.net_profit is not None else "-"
        roi = f"{r.roi_pct}%" if r.roi_pct is not None else "-"
        score = str(r.score) if not r.excluded else "-"
        priority = "ignore" if r.excluded else r.priority.value
        print(
            f"{r.watch_rule_id:<4} | {r.merchant:<14} | {r.product_name:<28} | "
            f"{str(r.purchase_price):>8} | {resale:>8} | {profit:>8} | {roi:>7} | "
            f"{score:>6} | {priority}"
        )
        if r.excluded and r.exclusion_reason:
            reason = r.exclusion_reason
            watch_rule = rules_by_id.get(r.watch_rule_id)
            if (
                reason == "no resale estimate available"
                and watch_rule is not None
                and watch_rule.resale_price_mode == "market"
                and market_registry is None
            ):
                reason = "market unavailable"
            print(f"     ({reason})")
    return 0


def cmd_purchases(args: argparse.Namespace) -> int:
    session = _get_session()
    attempts = crud.list_purchase_attempts(session, limit=args.limit)
    if not attempts:
        print("No purchase attempts recorded.")
        return 0

    header = (
        f"{'ID':<5} | {'Rule':<5} | {'Status':<28} | {'Price':>8} | {'Total':>8} | "
        f"{'Order ref':<16} | Created"
    )
    print(header)
    for a in attempts:
        total = str(a.total_cost) if a.total_cost is not None else "-"
        order_ref = a.order_reference or "-"
        print(
            f"{a.id:<5} | {a.watch_rule_id:<5} | {a.status:<28} | {str(a.observed_price):>8} | "
            f"{total:>8} | {order_ref:<16} | {a.created_at.isoformat()}"
        )
        if a.failure_reason:
            print(f"        ({a.failure_reason})")
    return 0


def cmd_purchase_status(args: argparse.Namespace) -> int:
    session = _get_session()
    policy = load_purchase_policy()

    print(f"PURCHASES_ENABLED:          {policy.enabled}")
    print(
        "PURCHASE_MAX_ORDER_EUR:     "
        f"{policy.max_order_eur if policy.max_order_eur is not None else 'not set'}"
    )
    print(
        "PURCHASE_MAX_DAILY_EUR:     "
        f"{policy.max_daily_eur if policy.max_daily_eur is not None else 'not set'}"
    )
    allowed = ", ".join(sorted(policy.allowed_merchant_domains)) or "(none)"
    print(f"PURCHASE_ALLOWED_MERCHANTS: {allowed}")
    print(f"PURCHASE_COOLDOWN_SECONDS:  {policy.cooldown_seconds}")

    since = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)
    spent_today = crud.sum_purchased_total_since(session, since)
    print(f"Spent in last 24h:          {spent_today}")

    attempts = crud.list_purchase_attempts(session, limit=1)
    if attempts:
        last = attempts[0]
        print(f"Most recent attempt:       #{last.id} status={last.status} ({last.created_at})")
    else:
        print("Most recent attempt:       none")
    return 0


def cmd_purchase_test(args: argparse.Namespace) -> int:
    """DRY RUN only: runs the full detection -> validation -> budget
    pipeline and prints what would happen, but never persists a
    PurchaseAttempt and never calls a connector's revalidate()/checkout()
    — see purchase/engine.py's module docstring for why those two steps
    are deliberately skipped here."""
    session = _get_session()
    rule = crud.get_watch_rule_by_listing_id(session, args.listing_id)
    if rule is None:
        print(f"No watch_rule found for listing_id={args.listing_id}")
        return 1

    registry = _build_registry()
    result = run_check_and_store(session, rule, registry)
    if not result.success:
        print(f"check failed: {result.error}")
        return 1

    policy = load_purchase_policy()
    intent = build_purchase_intent(rule, result.observation, result.match_result)
    total_cost = intent.observed_price * intent.quantity
    has_active, since_last, spent_today = build_decision_context(session, intent)
    merchant_domains = domains_for_merchant(intent.merchant)

    decision = evaluate_purchase_intent(
        watch_rule=rule,
        intent=intent,
        policy=policy,
        merchant_domains=merchant_domains,
        match_confidence=result.match_result.confidence,
        available=result.observation.available,
        total_cost=total_cost,
        has_active_attempt=has_active,
        seconds_since_last_attempt=since_last,
        spent_today=spent_today,
    )

    print("DRY RUN")
    print()
    print(f"Product: {intent.product_name}")
    print(f"Merchant: {intent.merchant}")
    print(f"Observed price: {intent.observed_price}")
    print(f"Final expected cost: {total_cost}")
    print(f"Max allowed: {rule.max_price}")
    print(f"Quantity: {intent.quantity}")
    print(f"Match confidence: {result.match_result.confidence}")
    print(f"Budget check: {'OK' if decision.proceed else 'FAILED'}")
    print()
    print(f"WOULD PURCHASE: {'YES' if decision.proceed else 'NO'}")
    if not decision.proceed:
        print(f"Reason: {decision.reason}")
    print()
    print("No transaction executed.")
    return 0


def _print_discovery_result(result) -> None:
    for outcome in result.merchants:
        if outcome.status == "unavailable":
            print(f"{outcome.merchant}: DISCOVERY_UNAVAILABLE ({outcome.detail})")
            continue
        if outcome.status == "error":
            print(f"{outcome.merchant}: error ({outcome.detail})")
            continue
        if not outcome.candidates:
            print(f"{outcome.merchant}: no listing found")
            continue
        for c in outcome.candidates:
            if c.verdict == "auto_link":
                state = "already monitored" if c.already_monitored else "LINKED"
                print(
                    f"{outcome.merchant}: {c.candidate_name!r} -> {c.verdict.upper()} "
                    f"({state}, confidence={c.confidence}) {c.candidate_url}"
                )
            else:
                print(
                    f"{outcome.merchant}: {c.candidate_name!r} -> {c.verdict.upper()} "
                    f"(confidence={c.confidence}) — {c.reason}"
                )


def cmd_add_product(args: argparse.Namespace) -> int:
    """Create/reuse a Product-level watch (Phase 22): one product, one
    shared max_total_price, discovered and monitored across every
    supported merchant automatically — no manual per-merchant WatchRule."""
    session = _get_session()

    try:
        max_price = _parse_positive_decimal("max-price", args.max_price)
        target_price = (
            _parse_positive_decimal("target-price", args.target_price)
            if args.target_price
            else None
        )
        max_quantity = _parse_positive_int("max-quantity", args.max_quantity or "1")
        monitoring_interval = _parse_check_interval(
            args.monitoring_interval or str(DEFAULT_AUTO_LINKED_CHECK_INTERVAL_SECONDS)
        )
    except ValueError as exc:
        print(f"Invalid value: {exc}")
        return 1

    product = crud.get_product_by_ean(session, args.ean) if args.ean else None
    if product is not None:
        print(f"Reusing existing product #{product.id} (matched by EAN {args.ean}).")
    else:
        product = crud.create_product(
            session,
            args.name,
            ean=args.ean,
            gtin=args.gtin,
            mpn=args.mpn,
            max_price=max_price,
            target_price=target_price,
            max_quantity=max_quantity,
        )
        print(f"Created product #{product.id}: {product.name}")

    print(f"Max total price: {product.max_price}")
    if product.target_price:
        print(f"Target price:    {product.target_price}")
    print()
    print("Running initial multi-merchant discovery...")
    print()

    registry = _build_discovery_registry()
    result = asyncio.run(
        run_discovery_for_product(session, product, registry, check_interval=monitoring_interval)
    )
    _print_discovery_result(result)
    return 0


def cmd_products(args: argparse.Namespace) -> int:
    session = _get_session()
    products = crud.list_products(session)
    if not products:
        print("No products watched.")
        return 0

    print(
        f"{'ID':<4} | {'Product':<40} | {'Max':>8} | {'Listings':>8} | {'Best Price':>10} | Status"
    )
    for p in products:
        rules = crud.list_watch_rules(session, product_id=p.id)
        best_price = None
        for r in rules:
            records = crud.list_observation_records_for_listing(session, r.listing_id)
            if records:
                last_price = records[-1].price
                if best_price is None or last_price < best_price:
                    best_price = last_price
        best_price_str = f"{best_price}€" if best_price is not None else "-"
        max_str = f"{p.max_price}€" if p.max_price is not None else "-"
        name = p.name if len(p.name) <= 40 else p.name[:37] + "..."
        print(
            f"{p.id:<4} | {name:<40} | {max_str:>8} | {len(rules):>8} | {best_price_str:>10} | "
            f"{p.status.upper()}"
        )
    return 0


def cmd_product(args: argparse.Namespace) -> int:
    session = _get_session()
    product = crud.get_product(session, args.id)
    if product is None:
        print(f"No product={args.id}")
        return 1

    print(f"Product: {product.name}")
    print()
    print("Identifiers:")
    print(f"  EAN: {product.ean or '-'}")
    print(f"  GTIN: {product.gtin or '-'}")
    print(f"  MPN: {product.mpn or '-'}")
    print()
    print(f"Max total: {product.max_price}")
    if product.target_price:
        print(f"Target: {product.target_price}")
    print(f"Max quantity: {product.max_quantity or 1}")
    print(f"Status: {product.status}")
    print(f"Discovery interval: {product.discovery_interval}s")
    last_discovery = product.last_discovery_at.isoformat() if product.last_discovery_at else "never"
    print(f"Last discovery: {last_discovery}")
    print()
    print("Listings:")
    rules = crud.list_watch_rules(session, product_id=product.id)
    if not rules:
        print("  none yet")
    for r in rules:
        listing = r.listing
        records = crud.list_observation_records_for_listing(session, listing.id)
        last = records[-1] if records else None
        if last is None:
            stock = "unknown"
        else:
            stock = "yes" if last.available else "no"
        print()
        print(f"  {listing.merchant.name}")
        print(f"    price: {last.price if last else 'unknown'}")
        print(f"    stock: {stock}")
        print(f"    last check: {last.observed_at.isoformat() if last else 'never'}")
        print(f"    watch_rule={r.id} enabled={r.enabled}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    session = _get_session()
    product = crud.get_product(session, args.id)
    if product is None:
        print(f"No product={args.id}")
        return 1

    registry = _build_discovery_registry()
    result = asyncio.run(run_discovery_for_product(session, product, registry))
    _print_discovery_result(result)
    return 0


def cmd_edit_product(args: argparse.Namespace) -> int:
    """Phase 25 — profitability-based purchase decisions, configurable
    per Product Watch (shared by every auto-discovered WatchRule under
    it, same as max_price/target_price already are — see
    engine.decision's effective_*() fallbacks)."""
    session = _get_session()
    product = crud.get_product(session, args.id)
    if product is None:
        print(f"No product={args.id}")
        return 1

    fields: dict[str, object] = {}
    try:
        if args.target_price is not None:
            fields["target_price"] = _parse_positive_decimal("target_price", args.target_price)
        if args.max_price is not None:
            fields["max_price"] = _parse_positive_decimal("max_price", args.max_price)
        if args.estimated_resale_price is not None:
            fields["estimated_resale_price"] = _parse_positive_decimal(
                "estimated_resale_price", args.estimated_resale_price
            )
        if args.resale_trusted is not None:
            fields["estimated_resale_trusted"] = _parse_bool("resale_trusted", args.resale_trusted)
        if args.platform_fee_pct is not None:
            fields["platform_fee_pct"] = _parse_fee_pct(args.platform_fee_pct)
        if args.fixed_fee is not None:
            fields["fixed_fee"] = _parse_non_negative_decimal("fixed_fee", args.fixed_fee)
        if args.shipping_cost is not None:
            fields["shipping_cost"] = _parse_non_negative_decimal(
                "shipping_cost", args.shipping_cost
            )
        if args.other_costs is not None:
            fields["other_costs"] = _parse_non_negative_decimal("other_costs", args.other_costs)
        if args.resale_price_mode is not None:
            fields["resale_price_mode"] = args.resale_price_mode
        if args.market_source is not None:
            fields["market_source"] = _parse_market_source(args.market_source)
        if args.minimum_net_profit is not None:
            fields["minimum_net_profit"] = _parse_non_negative_decimal(
                "minimum_net_profit", args.minimum_net_profit
            )
        if args.minimum_roi_pct is not None:
            fields["minimum_roi_pct"] = _parse_non_negative_decimal(
                "minimum_roi_pct", args.minimum_roi_pct
            )
        if args.minimum_resale_confidence is not None:
            fields["minimum_resale_confidence"] = _parse_resale_confidence(
                args.minimum_resale_confidence
            )

        effective_target = fields.get("target_price", product.target_price)
        effective_max = fields.get("max_price", product.max_price)
        _validate_price_order(effective_target, effective_max)

        effective_mode = fields.get("resale_price_mode", product.resale_price_mode)
        effective_source = fields.get("market_source", product.market_source)
        if effective_mode == "market" and effective_source is None:
            raise ValueError("resale_price_mode=market requires --market-source")
    except ValueError as exc:
        print(f"Invalid value: {exc}")
        return 1

    if not fields:
        print(
            "Nothing to update — pass at least one of --target-price/--max-price/"
            "--estimated-resale-price/--resale-trusted/--platform-fee-pct/--fixed-fee/"
            "--shipping-cost/--other-costs/--resale-price-mode/--market-source/"
            "--minimum-net-profit/--minimum-roi-pct/--minimum-resale-confidence."
        )
        return 1

    updated = crud.update_product(session, args.id, **fields)
    changes = ", ".join(f"{key}={value}" for key, value in fields.items())
    print(f"product={updated.id} updated: {changes}")
    return 0


def cmd_enable_product(args: argparse.Namespace) -> int:
    session = _get_session()
    product = crud.update_product(session, args.id, status="active")
    if product is None:
        print(f"No product={args.id}")
        return 1
    for rule in crud.list_watch_rules(session, product_id=product.id):
        crud.enable_watch_rule(session, rule.id)
    print(f"product={product.id} enabled.")
    return 0


def cmd_disable_product(args: argparse.Namespace) -> int:
    session = _get_session()
    product = crud.update_product(session, args.id, status="disabled")
    if product is None:
        print(f"No product={args.id}")
        return 1
    for rule in crud.list_watch_rules(session, product_id=product.id):
        crud.disable_watch_rule(session, rule.id)
    print(f"product={product.id} disabled.")
    return 0


def _worker_status() -> str:
    return pidfile.describe_status(pidfile.get_status(PID_FILE))


def cmd_status(args: argparse.Namespace) -> int:
    session = _get_session()
    active = crud.list_watch_rules(session, enabled=True)
    disabled = crud.list_watch_rules(session, enabled=False)
    print(f"active rules:   {len(active)}")
    print(f"disabled rules: {len(disabled)}")

    last_observation = crud.get_most_recent_observation(session)
    if last_observation is not None:
        print(
            f"last check:     {last_observation.observed_at.isoformat()} "
            f"(listing={last_observation.listing_id}, price={last_observation.price})"
        )
    else:
        print("last check:     never")

    last_event = crud.get_most_recent_event(session)
    if last_event is not None:
        print(
            f"last event:     {last_event.event_type} at {last_event.occurred_at.isoformat()} "
            f"(watch_rule={last_event.watch_rule_id})"
        )
    else:
        print("last event:     none")

    print(f"worker:         {_worker_status()}")

    counts = crud.notification_delivery_status_counts(session)
    last_success = crud.get_last_successful_delivery_at(session)
    print(
        "notifications:  "
        f"pending={counts.get('pending', 0) + counts.get('sending', 0)} "
        f"retryable_failed={counts.get('failed_retryable', 0)} "
        f"permanent_failed={counts.get('failed_permanent', 0)} "
        f"last_success={ensure_utc(last_success).isoformat() if last_success else 'never'}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage real WatchRules.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add a new watch rule.")
    add_parser.add_argument("--url", help="Product page URL")
    add_parser.add_argument("--merchant", help="Merchant display name")
    add_parser.add_argument("--name", help="Product name (overrides detection)")
    add_parser.add_argument("--external-id", dest="external_id", help="Merchant external_id")
    add_parser.add_argument("--target-price", dest="target_price")
    add_parser.add_argument("--max-price", dest="max_price")
    add_parser.add_argument("--check-interval", dest="check_interval")
    add_parser.add_argument("--max-quantity", dest="max_quantity")
    add_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    add_parser.set_defaults(func=cmd_add)

    edit_parser = subparsers.add_parser("edit", help="Edit an existing watch rule.")
    edit_parser.add_argument("id", type=int)
    edit_parser.add_argument("--target-price", dest="target_price")
    edit_parser.add_argument("--max-price", dest="max_price")
    edit_parser.add_argument("--check-interval", dest="check_interval")
    edit_parser.add_argument("--max-quantity", dest="max_quantity")
    edit_parser.add_argument("--estimated-resale-price", dest="estimated_resale_price")
    edit_parser.add_argument("--platform-fee-pct", dest="platform_fee_pct")
    edit_parser.add_argument("--fixed-fee", dest="fixed_fee")
    edit_parser.add_argument("--shipping-cost", dest="shipping_cost")
    edit_parser.add_argument("--other-costs", dest="other_costs")
    edit_parser.add_argument(
        "--resale-price-mode", dest="resale_price_mode", choices=["manual", "market"]
    )
    edit_parser.add_argument("--market-source", dest="market_source")
    edit_parser.set_defaults(func=cmd_edit)

    list_parser = subparsers.add_parser("list", help="List watch rules.")
    list_parser.add_argument("--status", choices=["enabled", "disabled", "all"], default="all")
    list_parser.set_defaults(func=cmd_list)

    for name, func, help_text in (
        ("show", cmd_show, "Show one watch rule."),
        ("enable", cmd_enable, "Enable a watch rule."),
        ("disable", cmd_disable, "Disable a watch rule."),
        ("delete", cmd_delete, "Delete a watch rule."),
        ("test", cmd_test, "Run one check immediately."),
        ("market", cmd_market, "Show market data diagnostics for a watch rule."),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("id", type=int)
        sub.set_defaults(func=func)

    opportunities_parser = subparsers.add_parser(
        "opportunities", help="Rank active watch rules by opportunity score."
    )
    opportunities_parser.add_argument("--top", type=int, default=None)
    opportunities_parser.add_argument(
        "--min-priority",
        dest="min_priority",
        choices=["ignore", "low", "medium", "high", "top"],
        default=None,
        type=str.lower,
    )
    opportunities_parser.set_defaults(func=cmd_opportunities)

    purchases_parser = subparsers.add_parser("purchases", help="List recorded purchase attempts.")
    purchases_parser.add_argument("--limit", type=int, default=None)
    purchases_parser.set_defaults(func=cmd_purchases)

    purchase_status_parser = subparsers.add_parser(
        "purchase-status", help="Show the automated-purchase policy and recent spend."
    )
    purchase_status_parser.set_defaults(func=cmd_purchase_status)

    purchase_test_parser = subparsers.add_parser(
        "purchase-test", help="Dry-run the purchase pipeline for one listing — never buys."
    )
    purchase_test_parser.add_argument("listing_id", type=int)
    purchase_test_parser.set_defaults(func=cmd_purchase_test)

    status_parser = subparsers.add_parser("status", help="Show overall system status.")
    status_parser.set_defaults(func=cmd_status)

    add_product_parser = subparsers.add_parser(
        "add-product",
        help="Watch a product by name/identifiers across every supported merchant.",
    )
    add_product_parser.add_argument("--name", required=True, help="Product name to search for")
    add_product_parser.add_argument("--max-price", dest="max_price", required=True)
    add_product_parser.add_argument("--target-price", dest="target_price")
    add_product_parser.add_argument("--max-quantity", dest="max_quantity")
    add_product_parser.add_argument(
        "--monitoring-interval",
        dest="monitoring_interval",
        help=(
            "check_interval in seconds for each auto-discovered WatchRule "
            f"(default {DEFAULT_AUTO_LINKED_CHECK_INTERVAL_SECONDS}, minimum "
            f"{MIN_CHECK_INTERVAL_SECONDS}) — e.g. 30, 60, 300"
        ),
    )
    add_product_parser.add_argument("--ean", help="Exact EAN/GTIN — highest-priority identifier")
    add_product_parser.add_argument("--gtin", help="GTIN, if different from --ean")
    add_product_parser.add_argument("--mpn", help="Manufacturer part number / SKU")
    add_product_parser.set_defaults(func=cmd_add_product)

    products_parser = subparsers.add_parser("products", help="List watched products.")
    products_parser.set_defaults(func=cmd_products)

    product_parser = subparsers.add_parser("product", help="Show one watched product in detail.")
    product_parser.add_argument("id", type=int)
    product_parser.set_defaults(func=cmd_product)

    discover_parser = subparsers.add_parser(
        "discover", help="Force an immediate multi-merchant discovery run for a product."
    )
    discover_parser.add_argument("id", type=int)
    discover_parser.set_defaults(func=cmd_discover)

    edit_product_parser = subparsers.add_parser(
        "edit-product",
        help="Edit a product watch's price/profitability config (shared by every listing).",
    )
    edit_product_parser.add_argument("id", type=int)
    edit_product_parser.add_argument("--target-price", dest="target_price")
    edit_product_parser.add_argument("--max-price", dest="max_price")
    edit_product_parser.add_argument("--estimated-resale-price", dest="estimated_resale_price")
    edit_product_parser.add_argument(
        "--resale-trusted",
        dest="resale_trusted",
        help="true/false — mark a manual estimated-resale-price as trusted enough for auto-buy",
    )
    edit_product_parser.add_argument("--platform-fee-pct", dest="platform_fee_pct")
    edit_product_parser.add_argument("--fixed-fee", dest="fixed_fee")
    edit_product_parser.add_argument("--shipping-cost", dest="shipping_cost")
    edit_product_parser.add_argument("--other-costs", dest="other_costs")
    edit_product_parser.add_argument(
        "--resale-price-mode", dest="resale_price_mode", choices=("manual", "market")
    )
    edit_product_parser.add_argument("--market-source", dest="market_source")
    edit_product_parser.add_argument(
        "--minimum-net-profit",
        dest="minimum_net_profit",
        help="switches this product watch to profitability mode: target_price/max_price stop "
        "being hard notification gates — see engine/decision.py",
    )
    edit_product_parser.add_argument("--minimum-roi-pct", dest="minimum_roi_pct")
    edit_product_parser.add_argument(
        "--minimum-resale-confidence",
        dest="minimum_resale_confidence",
        choices=("low", "medium", "high"),
    )
    edit_product_parser.set_defaults(func=cmd_edit_product)

    enable_product_parser = subparsers.add_parser(
        "enable-product", help="Enable a product watch and its WatchRules."
    )
    enable_product_parser.add_argument("id", type=int)
    enable_product_parser.set_defaults(func=cmd_enable_product)

    disable_product_parser = subparsers.add_parser(
        "disable-product", help="Disable a product watch and its WatchRules."
    )
    disable_product_parser.add_argument("id", type=int)
    disable_product_parser.set_defaults(func=cmd_disable_product)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
