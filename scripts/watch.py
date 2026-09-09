"""Manage real WatchRules from the command line — no direct SQLite editing.

Usage:
    python scripts/watch.py add [--url URL] [--merchant NAME] [--name NAME]
                                 [--external-id ID] [--target-price X]
                                 [--max-price X] [--check-interval N]
                                 [--max-quantity N] [--yes]
    python scripts/watch.py edit <id> [--target-price X] [--max-price X]
                                       [--check-interval N] [--max-quantity N]
    python scripts/watch.py list [--status enabled|disabled|all]
    python scripts/watch.py show <id>
    python scripts/watch.py enable <id>
    python scripts/watch.py disable <id>
    python scripts/watch.py delete <id>
    python scripts/watch.py test <id>
    python scripts/watch.py status

`add` with a Shopify product URL (any shop, not just Kairyu) tries
ShopifyConnector.get_product() first, so you don't have to type in what
can be detected (name, price, currency, stock, external_id, GTIN/MPN). If
detection fails (not Shopify, network error, page changed), it falls back
to asking for everything manually. Either way you confirm before anything
is written, unless --yes is passed.

Reusing an existing Listing when one already exists for the same
(merchant, external_id) is the only deduplication this does — no
purchase logic anywhere in this file.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.notify import notify_events_if_allowed
from connectors.base import ConnectorError, ConnectorProduct, ProductNotFoundError
from connectors.defaults import build_default_registry
from connectors.shopify import ShopifyConnector
from database import crud
from database.models import WatchRule
from database.session import create_all, get_engine, get_session_factory
from engine.monitoring import run_check_and_store
from engine.worker import MIN_CHECK_INTERVAL_SECONDS
from notifications.discord.client import DiscordNotifier
from notifications.discord.config import load_discord_config

PID_FILE = Path("data") / "worker.pid"


def _build_registry():
    """Thin alias kept for tests to monkeypatch a per-test registry
    without touching connectors/defaults.py."""
    return build_default_registry()


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


@dataclass
class DetectedProduct:
    merchant_name: str
    shop_domain: str
    handle: str
    connector_product: ConnectorProduct | None


def _detect_from_url(url: str, merchant_override: str | None) -> DetectedProduct:
    parsed = urlparse(url)
    shop_domain = parsed.netloc
    handle = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    merchant_name = merchant_override or shop_domain

    connector = ShopifyConnector(shop_domain=shop_domain, merchant_name=merchant_name)
    try:
        product = connector.get_product(handle)
    except (ConnectorError, ProductNotFoundError) as exc:
        print(f"Auto-detection failed ({exc}); please enter the product details manually.")
        product = None
    return DetectedProduct(
        merchant_name=merchant_name,
        shop_domain=shop_domain,
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
    if product_data is not None:
        print("Detected product:")
        print(f"  name:        {product_data.name}")
        print(f"  price:       {product_data.price} {product_data.currency}")
        print(f"  in stock:    {product_data.available}")
        print(f"  external_id: {product_data.external_id}")
        print(f"  ean/gtin:    {product_data.ean}")
        print(f"  mpn:         {product_data.mpn}")

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

        effective_target = fields.get("target_price", rule.target_price)
        effective_max = fields.get("max_price", rule.max_price)
        _validate_price_order(effective_target, effective_max)
    except ValueError as exc:
        print(f"Invalid value: {exc}")
        return 1

    if not fields:
        print(
            "Nothing to update — pass at least one of "
            "--target-price/--max-price/--check-interval/--max-quantity."
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

    config = load_discord_config()
    async with DiscordNotifier(config) as notifier:
        decision = await notify_events_if_allowed(
            rule, result.observation, result.match_result, result.events, notifier
        )

    event_names = [e.event_type.value for e in result.events] or ["none"]
    print(f"events:   {', '.join(event_names)}")
    print(
        f"decision: {decision.decision_code.value} (allowed={decision.allowed}) — {decision.reason}"
    )
    notified = decision.allowed and bool(result.events)
    print(f"notified: {notified}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    session = _get_session()
    rule = crud.get_watch_rule(session, args.id)
    if rule is None:
        print(f"No watch_rule={args.id}")
        return 1
    return asyncio.run(_run_test(session, rule))


def _worker_status() -> str:
    if not PID_FILE.exists():
        return "not running"
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        return "unknown (invalid pid file)"
    try:
        os.kill(pid, 0)
    except OSError:
        return "not running (stale pid file)"
    return f"running (pid={pid})"


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
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("id", type=int)
        sub.set_defaults(func=func)

    status_parser = subparsers.add_parser("status", help="Show overall system status.")
    status_parser.set_defaults(func=cmd_status)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
