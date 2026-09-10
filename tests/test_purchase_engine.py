"""evaluate_purchase_intent is pure: no DB, no network, no connector — so
these tests build every input by hand rather than through the app/DB
layers. See tests/test_purchase_engine_integration.py for the full,
DB-backed attempt_purchase() flow with a fake connector.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from database.models import Product, WatchRule
from purchase.config import PurchasePolicy
from purchase.engine import build_purchase_intent, evaluate_purchase_intent
from purchase.models import PurchaseIntent, PurchaseStatus


def _watch_rule(**overrides: object) -> WatchRule:
    product = Product(id=1, name="ETB Chaos Ascendant FR")
    defaults: dict[str, object] = dict(
        id=1,
        product_id=1,
        listing_id=1,
        product=product,
        check_interval=300,
        max_quantity=1,
        enabled=True,
        max_price=Decimal("60"),
        target_price=None,
        resale_price_mode="manual",
    )
    defaults.update(overrides)
    return WatchRule(**defaults)  # type: ignore[arg-type]


def _policy(**overrides: object) -> PurchasePolicy:
    defaults: dict[str, object] = dict(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({"kairyu.fr"}),
        cooldown_seconds=0,
    )
    defaults.update(overrides)
    return PurchasePolicy(**defaults)  # type: ignore[arg-type]


def _intent(watch_rule: WatchRule, *, price: str = "59.90", quantity: int = 1) -> PurchaseIntent:
    return PurchaseIntent(
        watch_rule_id=watch_rule.id,
        product_id=watch_rule.product_id,
        listing_id=watch_rule.listing_id,
        merchant="Kairyu",
        product_name=watch_rule.product.name,
        url="https://kairyu.fr/products/etb-chaos-ascendant-fr",
        observed_price=Decimal(price),
        max_price_allowed=watch_rule.max_price or Decimal("0"),
        quantity=quantity,
        match_confidence=100,
        created_at=datetime.now(UTC),
    )


def _evaluate(
    watch_rule: WatchRule,
    intent: PurchaseIntent,
    policy: PurchasePolicy,
    *,
    match_confidence: int = 100,
    available: bool = True,
    total_cost: Decimal | None = None,
    has_active_attempt: bool = False,
    seconds_since_last_attempt: float | None = None,
    spent_today: Decimal = Decimal("0"),
):
    if total_cost is None:
        total_cost = intent.observed_price * intent.quantity
    return evaluate_purchase_intent(
        watch_rule=watch_rule,
        intent=intent,
        policy=policy,
        merchant_domains=("kairyu.fr", "www.kairyu.fr"),
        match_confidence=match_confidence,
        available=available,
        total_cost=total_cost,
        has_active_attempt=has_active_attempt,
        seconds_since_last_attempt=seconds_since_last_attempt,
        spent_today=spent_today,
    )


def test_build_purchase_intent_from_watch_rule_and_observation() -> None:
    from products.matcher import MatchResult
    from products.observation import ProductObservation

    watch_rule = _watch_rule()
    obs = ProductObservation(
        merchant="Kairyu",
        external_id="etb-1",
        name="ETB Chaos Ascendant FR",
        price=Decimal("59.90"),
        currency="EUR",
        available=True,
        url="https://kairyu.fr/products/etb-chaos-ascendant-fr",
        observed_at=datetime.now(UTC),
    )
    match = MatchResult(matched=True, confidence=100, method="ean_exact", reason="test")

    intent = build_purchase_intent(watch_rule, obs, match)

    assert intent.watch_rule_id == 1
    assert intent.observed_price == Decimal("59.90")
    assert intent.quantity == 1
    assert intent.max_price_allowed == Decimal("60")
    assert intent.match_confidence == 100


def test_happy_path_all_checks_pass() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule)
    policy = _policy()

    decision = _evaluate(watch_rule, intent, policy)

    assert decision.proceed is True
    assert decision.status == PurchaseStatus.CREATED


def test_price_exactly_at_max_is_allowed() -> None:
    watch_rule = _watch_rule(max_price=Decimal("59.90"))
    intent = _intent(watch_rule, price="59.90")
    policy = _policy()

    decision = _evaluate(watch_rule, intent, policy)

    assert decision.proceed is True


def test_price_one_cent_above_max_is_refused() -> None:
    watch_rule = _watch_rule(max_price=Decimal("59.90"))
    intent = _intent(watch_rule, price="59.91")
    policy = _policy()

    decision = _evaluate(watch_rule, intent, policy)

    assert decision.proceed is False


def test_fees_pushing_total_above_max_is_refused() -> None:
    """59 EUR product + 6 EUR shipping = 65 EUR total, max_price=60 -> refuse."""
    watch_rule = _watch_rule(max_price=Decimal("60"))
    intent = _intent(watch_rule, price="59")
    policy = _policy()

    decision = _evaluate(watch_rule, intent, policy, total_cost=Decimal("65"))

    assert decision.proceed is False
    assert "exceeds" in decision.reason


def test_disabled_rule_refuses() -> None:
    watch_rule = _watch_rule(enabled=False)
    intent = _intent(watch_rule)
    policy = _policy()

    decision = _evaluate(watch_rule, intent, policy)

    assert decision.proceed is False
    assert "disabled" in decision.reason


def test_purchases_disabled_refuses() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule)
    policy = _policy(enabled=False)

    decision = _evaluate(watch_rule, intent, policy)

    assert decision.proceed is False
    assert "PURCHASES_ENABLED" in decision.reason


def test_merchant_not_allowed_refuses() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule)
    policy = _policy(allowed_merchant_domains=frozenset({"relictcg.com"}))

    decision = _evaluate(watch_rule, intent, policy)

    assert decision.proceed is False
    assert "PURCHASE_ALLOWED_MERCHANTS" in decision.reason


def test_out_of_stock_refuses() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule)
    policy = _policy()

    decision = _evaluate(watch_rule, intent, policy, available=False)

    assert decision.proceed is False
    assert "out of stock" in decision.reason.lower()


def test_bad_match_confidence_refuses() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule)
    policy = _policy()

    decision = _evaluate(watch_rule, intent, policy, match_confidence=50)

    assert decision.proceed is False
    assert "match confidence" in decision.reason.lower()


def test_no_max_price_configured_refuses() -> None:
    from dataclasses import replace

    watch_rule = _watch_rule(max_price=None)
    intent = replace(_intent(watch_rule), max_price_allowed=Decimal("0"))
    policy = _policy()

    decision = _evaluate(watch_rule, intent, policy)

    assert decision.proceed is False
    assert "no max_price" in decision.reason


def test_quantity_above_max_quantity_refuses() -> None:
    watch_rule = _watch_rule(max_quantity=1, max_price=Decimal("200"))
    intent = _intent(watch_rule, quantity=2)
    policy = _policy()

    decision = _evaluate(watch_rule, intent, policy, total_cost=Decimal("119.80"))

    assert decision.proceed is False
    assert "max_quantity" in decision.reason


def test_per_order_budget_exceeded_refuses() -> None:
    watch_rule = _watch_rule(max_price=Decimal("200"))
    intent = _intent(watch_rule, price="150")
    policy = _policy(max_order_eur=Decimal("100"))

    decision = _evaluate(watch_rule, intent, policy)

    assert decision.proceed is False
    assert "PURCHASE_MAX_ORDER_EUR" in decision.reason


def test_daily_budget_exceeded_refuses() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule, price="50")
    policy = _policy(max_daily_eur=Decimal("100"))

    decision = _evaluate(watch_rule, intent, policy, spent_today=Decimal("60"))

    assert decision.proceed is False
    assert "PURCHASE_MAX_DAILY_EUR" in decision.reason


def test_daily_budget_not_exceeded_when_within_limit() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule, price="30")
    policy = _policy(max_daily_eur=Decimal("100"))

    decision = _evaluate(watch_rule, intent, policy, spent_today=Decimal("60"))

    assert decision.proceed is True


def test_active_duplicate_attempt_refuses() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule)
    policy = _policy()

    decision = _evaluate(watch_rule, intent, policy, has_active_attempt=True)

    assert decision.proceed is False
    assert "active purchase attempt" in decision.reason


def test_cooldown_active_refuses() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule)
    policy = _policy(cooldown_seconds=300)

    decision = _evaluate(watch_rule, intent, policy, seconds_since_last_attempt=100)

    assert decision.proceed is False
    assert "Cooldown" in decision.reason


def test_cooldown_elapsed_allows() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule)
    policy = _policy(cooldown_seconds=300)

    decision = _evaluate(watch_rule, intent, policy, seconds_since_last_attempt=301)

    assert decision.proceed is True


def test_no_previous_attempt_bypasses_cooldown() -> None:
    watch_rule = _watch_rule()
    intent = _intent(watch_rule)
    policy = _policy(cooldown_seconds=300)

    decision = _evaluate(watch_rule, intent, policy, seconds_since_last_attempt=None)

    assert decision.proceed is True
