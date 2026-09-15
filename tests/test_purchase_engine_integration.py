"""Full attempt_purchase() flow against the in-memory test DB, with a
fake PurchaseConnector standing in for a real merchant — no real network,
no real Discord (FakeNotifier only implements send_embed), no real money
ever moves in any test here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from database import crud
from products.matcher import MatchResult
from products.observation import ProductObservation
from purchase.base import (
    AutomatedCheckoutUnsupportedError,
    CheckoutResult,
    HumanActionRequiredError,
    PurchaseConnector,
)
from purchase.config import PurchasePolicy
from purchase.engine import attempt_purchase
from purchase.models import PurchaseStatus, RevalidationResult
from purchase.registry import PurchaseConnectorRegistry


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


class FakeConnector(PurchaseConnector):
    def __init__(
        self,
        *,
        revalidate_result: RevalidationResult | None = None,
        revalidate_error: Exception | None = None,
        checkout_result: CheckoutResult | None = None,
        checkout_error: Exception | None = None,
    ) -> None:
        self._revalidate_result = revalidate_result or RevalidationResult(
            available=True, price=Decimal("59.90"), shipping_cost=None, quantity_available=None
        )
        self._revalidate_error = revalidate_error
        self._checkout_result = checkout_result or CheckoutResult(
            success=True,
            order_reference="ORDER-123",
            final_price=Decimal("59.90"),
            shipping_cost=Decimal("0"),
            total_cost=Decimal("59.90"),
            failure_reason=None,
        )
        self._checkout_error = checkout_error
        self.revalidate_calls = 0
        self.checkout_calls = 0

    def revalidate(self, intent):
        self.revalidate_calls += 1
        if self._revalidate_error is not None:
            raise self._revalidate_error
        return self._revalidate_result

    def checkout(self, intent, revalidated):
        self.checkout_calls += 1
        if self._checkout_error is not None:
            raise self._checkout_error
        return self._checkout_result


def _seed_rule(
    session: Session, *, max_price: str = "60", max_quantity: int = 1, enabled: bool = True
) -> tuple[int, int]:
    product = crud.create_product(session, "ETB Chaos Ascendant FR")
    merchant = crud.create_merchant(session, "Kairyu")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://kairyu.fr/products/etb-chaos-ascendant-fr",
        external_id="etb-1",
    )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=60,
        max_quantity=max_quantity,
        max_price=Decimal(max_price),
        enabled=enabled,
    )
    return rule.id, listing.id


def _observation(price: str = "59.90", available: bool = True) -> ProductObservation:
    return ProductObservation(
        merchant="Kairyu",
        external_id="etb-1",
        name="ETB Chaos Ascendant FR",
        price=Decimal(price),
        currency="EUR",
        available=available,
        url="https://kairyu.fr/products/etb-chaos-ascendant-fr",
        observed_at=datetime.now(UTC),
    )


def _match(confidence: int = 100) -> MatchResult:
    return MatchResult(matched=True, confidence=confidence, method="ean_exact", reason="test")


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


def _registry(connector: PurchaseConnector) -> PurchaseConnectorRegistry:
    registry = PurchaseConnectorRegistry()
    registry.register("Kairyu", connector)
    return registry


def test_purchase_success_persists_and_notifies(session: Session) -> None:
    rule_id, _listing_id = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector()
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.PURCHASED
    assert outcome.order_reference == "ORDER-123"
    attempts = crud.list_purchase_attempts(session)
    assert len(attempts) == 1
    assert attempts[0].status == "purchased"
    assert attempts[0].order_reference == "ORDER-123"
    titles = [e.title for e in notifier.sent_embeds]
    assert "⚡ AUTO PURCHASE STARTED" in titles
    assert "✅ PURCHASED" in titles


def test_purchase_disabled_creates_no_attempt_row(session: Session) -> None:
    rule_id, _ = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector()
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(enabled=False),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED
    assert crud.list_purchase_attempts(session) == []
    assert notifier.sent_embeds == []
    assert connector.revalidate_calls == 0
    assert connector.checkout_calls == 0


def test_checkout_unsupported_reports_status_and_alerts(session: Session) -> None:
    rule_id, _ = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        revalidate_error=AutomatedCheckoutUnsupportedError("Kairyu has no automated checkout")
    )
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.AUTOMATED_CHECKOUT_UNSUPPORTED
    attempts = crud.list_purchase_attempts(session)
    assert attempts[0].status == "automated_checkout_unsupported"
    assert "🚨 BUY NOW" in [e.title for e in notifier.sent_embeds]


def test_captcha_during_revalidation_yields_human_action_required(session: Session) -> None:
    rule_id, _ = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        revalidate_error=HumanActionRequiredError("Cloudflare challenge detected")
    )
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.HUMAN_ACTION_REQUIRED
    assert "🟠 HUMAN ACTION REQUIRED" in [e.title for e in notifier.sent_embeds]


def test_3ds_during_checkout_yields_human_action_required(session: Session) -> None:
    rule_id, _ = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        checkout_error=HumanActionRequiredError("3-D Secure confirmation required")
    )
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.HUMAN_ACTION_REQUIRED


def test_stock_disappears_during_revalidation_cancels(session: Session) -> None:
    rule_id, _ = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        revalidate_result=RevalidationResult(
            available=False, price=Decimal("59.90"), shipping_cost=None, quantity_available=None
        )
    )
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED
    assert connector.checkout_calls == 0


def test_price_increase_during_revalidation_cancels(session: Session) -> None:
    """59.90 observed, max_price=60 — but revalidation reveals 65 total
    (price + shipping) once in the cart: must refuse, never check out."""
    rule_id, _ = _seed_rule(session, max_price="60")
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        revalidate_result=RevalidationResult(
            available=True, price=Decimal("59"), shipping_cost=Decimal("6"), quantity_available=None
        )
    )
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(price="59"),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED
    assert connector.checkout_calls == 0


def test_insufficient_quantity_at_revalidation_cancels(session: Session) -> None:
    rule_id, _ = _seed_rule(session, max_quantity=2, max_price="200")
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        revalidate_result=RevalidationResult(
            available=True, price=Decimal("59.90"), shipping_cost=None, quantity_available=1
        )
    )
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED
    assert connector.checkout_calls == 0


def test_checkout_failure_reports_failed(session: Session) -> None:
    rule_id, _ = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        checkout_result=CheckoutResult(
            success=False,
            order_reference=None,
            final_price=None,
            shipping_cost=None,
            total_cost=None,
            failure_reason="card declined by merchant gateway",
        )
    )
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.FAILED
    assert "❌ PURCHASE FAILED" in [e.title for e in notifier.sent_embeds]


def test_duplicate_active_attempt_is_refused_not_double_purchased(session: Session) -> None:
    rule_id, listing_id = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    crud.create_purchase_attempt(
        session,
        watch_rule_id=rule_id,
        listing_id=listing_id,
        product_id=rule.product_id,
        status="checkout_started",
        observed_price=Decimal("59.90"),
        max_price_allowed=Decimal("60"),
        quantity=1,
    )
    connector = FakeConnector()
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED
    assert connector.checkout_calls == 0
    # Only the pre-existing attempt exists — no second row was created.
    assert len(crud.list_purchase_attempts(session)) == 1


def test_cooldown_blocks_second_attempt_after_recent_one(session: Session) -> None:
    rule_id, listing_id = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    crud.create_purchase_attempt(
        session,
        watch_rule_id=rule_id,
        listing_id=listing_id,
        product_id=rule.product_id,
        status="failed",
        observed_price=Decimal("59.90"),
        max_price_allowed=Decimal("60"),
        quantity=1,
    )
    connector = FakeConnector()
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(cooldown_seconds=3600),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED
    assert connector.checkout_calls == 0


def test_monitoring_survives_a_connector_bug(session: Session) -> None:
    """A raw, unexpected exception from a connector must never propagate
    out of attempt_purchase() — the rest of the worker keeps running."""
    rule_id, _ = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(revalidate_error=RuntimeError("unexpected bug"))
    notifier = FakeNotifier()

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
        )
    )

    assert outcome.status == PurchaseStatus.FAILED


def test_no_payment_data_field_exists_on_purchase_attempt(session: Session) -> None:
    """Structural guarantee: PurchaseAttempt has no column that could
    ever hold a card number, CVV, password, or payment token."""
    from database.models import PurchaseAttempt

    column_names = {c.name for c in PurchaseAttempt.__table__.columns}
    forbidden_substrings = ("card", "cvv", "password", "token", "cookie", "secret")
    for name in column_names:
        assert not any(bad in name.lower() for bad in forbidden_substrings), name


# --- Phase 25: profitability mode re-checked against the REAL revalidated
# total (item + real shipping), not just the item price the alert used ----


def _profitability_opportunity(rule, *, resale_price: str, item_price: str):
    from engine.opportunity import build_opportunity_inputs, evaluate_opportunity

    config, thresholds = build_opportunity_inputs(rule, Decimal(resale_price))
    return evaluate_opportunity(Decimal(item_price), config, thresholds)


def test_profitability_mode_proceeds_when_revalidated_total_is_profitable(
    session: Session,
) -> None:
    """The exact spec example: item 55.99 + shipping 4.00 = 59.99
    acquisition, resale 120 -> STRONG BUY, proceeds through to checkout."""
    from market_data.estimator import Confidence

    rule_id, _ = _seed_rule(session, max_price="1000")
    crud.update_watch_rule(session, rule_id, max_price=None, minimum_net_profit=Decimal("20"))
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        revalidate_result=RevalidationResult(
            available=True,
            price=Decimal("55.99"),
            shipping_cost=Decimal("4.00"),
            quantity_available=None,
        )
    )
    notifier = FakeNotifier()
    opportunity = _profitability_opportunity(rule, resale_price="120", item_price="55.99")

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(price="55.99"),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
            opportunity=opportunity,
            resale_confidence=Confidence.HIGH,
        )
    )

    assert outcome.status == PurchaseStatus.PURCHASED
    assert connector.checkout_calls == 1


def test_profitability_mode_cancels_when_revalidated_total_is_not_profitable(
    session: Session,
) -> None:
    """Same acquisition total (59.99), but a resale estimate too low to
    clear minimum_net_profit -> cancelled before checkout, real money
    never at risk regardless."""
    from market_data.estimator import Confidence

    rule_id, _ = _seed_rule(session, max_price="1000")
    crud.update_watch_rule(session, rule_id, max_price=None, minimum_net_profit=Decimal("20"))
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        revalidate_result=RevalidationResult(
            available=True,
            price=Decimal("55.99"),
            shipping_cost=Decimal("4.00"),
            quantity_available=None,
        )
    )
    notifier = FakeNotifier()
    # resale=65 vs acquisition ~60 -> profit ~5, below the 20 minimum.
    opportunity = _profitability_opportunity(rule, resale_price="65", item_price="55.99")

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(price="55.99"),
            _match(),
            _policy(),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
            opportunity=opportunity,
            resale_confidence=Confidence.HIGH,
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED
    assert connector.checkout_calls == 0
    assert "profitability" in outcome.reason.lower()


# --- Phase 26 audit, section 22: the kill switch is "obligatoire" tested
# against the single most favorable case there is — a STRONG_BUY, in
# stock, highly profitable opportunity — to prove PURCHASES_ENABLED=false
# beats even that. ---------------------------------------------------------


def test_kill_switch_blocks_strong_buy_in_stock_high_profit_opportunity(
    session: Session,
) -> None:
    """Same exact STRONG_BUY fixture as
    test_profitability_mode_proceeds_when_revalidated_total_is_profitable
    (item 55.99 + shipping 4.00, resale 120, HIGH confidence — a real
    STRONG_BUY recommendation) — the only difference is
    PURCHASES_ENABLED=false. Must still refuse, and the connector must
    never be touched at all: not revalidate(), not checkout()."""
    from market_data.estimator import Confidence

    rule_id, _ = _seed_rule(session, max_price="1000")
    crud.update_watch_rule(session, rule_id, max_price=None, minimum_net_profit=Decimal("20"))
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        revalidate_result=RevalidationResult(
            available=True,
            price=Decimal("55.99"),
            shipping_cost=Decimal("4.00"),
            quantity_available=None,
        )
    )
    notifier = FakeNotifier()
    opportunity = _profitability_opportunity(rule, resale_price="120", item_price="55.99")
    assert opportunity is not None
    from engine.opportunity import OpportunityStatus

    assert opportunity.status == OpportunityStatus.STRONG_BUY_CANDIDATE  # confirms the setup

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(price="55.99", available=True),
            _match(confidence=100),
            _policy(enabled=False),  # the kill switch
            _registry(connector),
            ("kairyu.fr",),
            notifier,
            opportunity=opportunity,
            resale_confidence=Confidence.HIGH,
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED
    assert "PURCHASES_ENABLED" in outcome.reason
    assert connector.revalidate_calls == 0
    assert connector.checkout_calls == 0
    assert crud.list_purchase_attempts(session) == []
    assert notifier.sent_embeds == []


def test_kill_switch_turned_off_mid_attempt_still_aborts_before_checkout(
    session: Session,
) -> None:
    """Phase 33 hardening: the top-of-function policy passed in says
    enabled=True (so this attempt gets past every earlier gate, all the
    way through a successful revalidate()) — but policy_provider (what
    app/worker.py wires to purchase.config.load_purchase_policy in
    production) reports the switch is now off by the time checkout would
    start. Must abort with zero calls to checkout() — "vérifié juste
    avant tout chemin transactionnel dangereux, pas seulement au
    démarrage."."""
    rule_id, _ = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector()
    notifier = FakeNotifier()

    def _policy_flipped_off() -> PurchasePolicy:
        return _policy(enabled=False)

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(),
            _match(),
            _policy(enabled=True),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
            policy_provider=_policy_flipped_off,
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED
    assert "PURCHASES_ENABLED" in outcome.reason
    assert connector.revalidate_calls == 1  # got this far
    assert connector.checkout_calls == 0  # but never actually checked out
    purchased = [a for a in crud.list_purchase_attempts(session) if a.status == "purchased"]
    assert purchased == []


def test_stale_resale_estimate_blocks_purchase(session: Session) -> None:
    """Phase 33 section 21: a manual resale price set 2 days ago, with
    PURCHASE_MAX_RESALE_AGE_SECONDS set to 1 hour, must be rejected as
    stale — never trusted forever just because it once looked good."""
    from datetime import UTC, datetime, timedelta

    from market_data.estimator import Confidence

    rule_id, _ = _seed_rule(session, max_price="1000")
    crud.update_watch_rule(
        session,
        rule_id,
        max_price=None,
        minimum_net_profit=Decimal("20"),
        estimated_resale_price=Decimal("120"),
        estimated_resale_trusted=True,
    )
    # Back-date the stamp update_watch_rule just auto-set, simulating a
    # resale estimate that was fresh once but is now genuinely old.
    crud.update_watch_rule(
        session, rule_id, resale_updated_at=datetime.now(UTC) - timedelta(days=2)
    )
    rule = crud.get_watch_rule(session, rule_id)
    connector = FakeConnector(
        revalidate_result=RevalidationResult(
            available=True,
            price=Decimal("55.99"),
            shipping_cost=Decimal("4.00"),
            quantity_available=None,
        )
    )
    notifier = FakeNotifier()
    opportunity = _profitability_opportunity(rule, resale_price="120", item_price="55.99")

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(price="55.99"),
            _match(),
            _policy(max_resale_age_seconds=3600),  # 1 hour
            _registry(connector),
            ("kairyu.fr",),
            notifier,
            opportunity=opportunity,
            resale_confidence=Confidence.HIGH,
        )
    )

    assert outcome.status == PurchaseStatus.CANCELLED
    assert "STALE_MARKET_DATA" in outcome.reason
    assert connector.revalidate_calls == 0
    assert connector.checkout_calls == 0


def test_fresh_resale_estimate_is_not_blocked_by_staleness_policy(session: Session) -> None:
    """Same setup, but the resale estimate was just set — must proceed
    normally even with a strict max_resale_age_seconds configured."""
    from market_data.estimator import Confidence

    rule_id, _ = _seed_rule(session, max_price="1000")
    crud.update_watch_rule(
        session,
        rule_id,
        max_price=None,
        minimum_net_profit=Decimal("20"),
        estimated_resale_price=Decimal("120"),
        estimated_resale_trusted=True,
    )
    rule = crud.get_watch_rule(session, rule_id)
    assert rule.resale_updated_at is not None  # auto-stamped just now
    connector = FakeConnector(
        revalidate_result=RevalidationResult(
            available=True,
            price=Decimal("55.99"),
            shipping_cost=Decimal("4.00"),
            quantity_available=None,
        )
    )
    notifier = FakeNotifier()
    opportunity = _profitability_opportunity(rule, resale_price="120", item_price="55.99")

    outcome = asyncio.run(
        attempt_purchase(
            session,
            rule,
            _observation(price="55.99"),
            _match(),
            _policy(max_resale_age_seconds=3600),
            _registry(connector),
            ("kairyu.fr",),
            notifier,
            opportunity=opportunity,
            resale_confidence=Confidence.HIGH,
        )
    )

    assert outcome.status == PurchaseStatus.PURCHASED


def test_reconcile_orphaned_purchase_attempts_marks_them_failed(session: Session) -> None:
    """Phase 33 section 24: a PurchaseAttempt stuck in checkout_started
    (simulating a worker that died mid-attempt) must be marked failed at
    startup, not left blocking every future attempt at that product
    forever."""
    from purchase.engine import reconcile_orphaned_purchase_attempts

    rule_id, listing_id = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    stuck = crud.create_purchase_attempt(
        session,
        watch_rule_id=rule_id,
        listing_id=listing_id,
        product_id=rule.product_id,
        status="checkout_started",
        observed_price=Decimal("59.90"),
        max_price_allowed=Decimal("60"),
        quantity=1,
    )

    reconciled_count = reconcile_orphaned_purchase_attempts(session)

    assert reconciled_count == 1
    refreshed = crud.list_purchase_attempts(session)[0]
    assert refreshed.id == stuck.id
    assert refreshed.status == "failed"
    assert "orphaned" in refreshed.failure_reason.lower()
    # The product is now free for a fresh attempt.
    assert crud.get_active_purchase_attempt_for_listing(session, listing_id) is None
    assert crud.get_blocking_purchase_attempts_for_product(session, rule.product_id) == []


def test_reconcile_is_a_no_op_when_nothing_is_stuck(session: Session) -> None:
    from purchase.engine import reconcile_orphaned_purchase_attempts

    _seed_rule(session)

    assert reconcile_orphaned_purchase_attempts(session) == 0
