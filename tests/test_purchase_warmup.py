"""purchase/warmup.py — Phase 35 section 14. No real network: every
connector under test is a fake standing in for a real one."""

from __future__ import annotations

from purchase.base import PurchaseConnector
from purchase.registry import PurchaseConnectorRegistry
from purchase.warmup import warm_up_purchase_connectors


class _WarmableConnector(PurchaseConnector):
    def __init__(self) -> None:
        self.warm_up_calls = 0

    def warm_up(self) -> None:
        self.warm_up_calls += 1

    def revalidate(self, intent):
        raise NotImplementedError

    def checkout(self, intent, revalidated):
        raise NotImplementedError


class _FailingWarmupConnector(PurchaseConnector):
    def warm_up(self) -> None:
        raise RuntimeError("merchant unreachable")

    def revalidate(self, intent):
        raise NotImplementedError

    def checkout(self, intent, revalidated):
        raise NotImplementedError


class _DefaultConnector(PurchaseConnector):
    """Never overrides warm_up() — must fall back to the base no-op."""

    def revalidate(self, intent):
        raise NotImplementedError

    def checkout(self, intent, revalidated):
        raise NotImplementedError


def test_warm_up_calls_every_registered_connector() -> None:
    registry = PurchaseConnectorRegistry()
    a = _WarmableConnector()
    b = _WarmableConnector()
    registry.register("A", a)
    registry.register("B", b)

    results = warm_up_purchase_connectors(registry)

    assert a.warm_up_calls == 1
    assert b.warm_up_calls == 1
    assert results == {"A": True, "B": True}


def test_a_failing_warm_up_is_reported_not_raised() -> None:
    registry = PurchaseConnectorRegistry()
    registry.register("Broken", _FailingWarmupConnector())
    registry.register("Fine", _WarmableConnector())

    results = warm_up_purchase_connectors(registry)

    assert results["Broken"] is False
    assert results["Fine"] is True


def test_default_warm_up_is_a_safe_no_op() -> None:
    connector = _DefaultConnector()

    connector.warm_up()  # must not raise
