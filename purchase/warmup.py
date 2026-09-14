"""Phase 35 section 14: pre-drop connection warming.

Calls each registered PurchaseConnector's own warm_up() (default: no-op
— see purchase/base.py) once. Retailer-safe by construction: warm_up()
implementations never touch a cart, never reserve stock, never create an
order — the same read-only, non-transactional posture as this project's
whole monitoring path. A connector that fails to warm up is logged, never
raised — a missed optimization must never crash the worker.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from purchase.registry import PurchaseConnectorRegistry

logger = logging.getLogger(__name__)


def warm_up_purchase_connectors(registry: PurchaseConnectorRegistry) -> dict[str, bool]:
    """Returns {merchant_name: warmed_successfully} for every registered
    connector — callers (e.g. app/main_worker.py at startup) can log or
    ignore the result; nothing here ever raises."""
    results: dict[str, bool] = {}
    for name in registry.names():
        connector = registry.get(name)
        try:
            connector.warm_up()
            results[name] = True
        except Exception:  # noqa: BLE001 - warm-up must never crash the worker
            logger.warning("warm_up failed for merchant=%s", name, exc_info=True)
            results[name] = False
    return results
