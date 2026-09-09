"""Fully local, deterministic fake merchant — no network access.

Lets the rest of the bot be built and tested without depending on any real
e-commerce site.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

from connectors.base import BaseConnector, ConnectorProduct, ProductNotFoundError

_REQUIRED_FIELDS = ("name", "price", "available", "seller", "url")


def _build_product(external_id: str, data: Mapping[str, object]) -> ConnectorProduct:
    missing = [field for field in _REQUIRED_FIELDS if field not in data]
    if missing:
        raise ValueError(
            f"FakeStoreConnector product {external_id!r} is missing required field(s): "
            f"{', '.join(missing)}"
        )
    return ConnectorProduct(
        external_id=str(data.get("external_id", external_id)),
        name=data["name"],  # type: ignore[arg-type]
        price=data["price"],  # type: ignore[arg-type]
        currency=data.get("currency", "EUR"),  # type: ignore[arg-type]
        available=data["available"],  # type: ignore[arg-type]
        seller=data["seller"],  # type: ignore[arg-type]
        url=data["url"],  # type: ignore[arg-type]
        ean=data.get("ean"),  # type: ignore[arg-type]
        mpn=data.get("mpn"),  # type: ignore[arg-type]
    )


class FakeStoreConnector(BaseConnector):
    """In-memory merchant simulator for tests and local development.

    `products` maps an external_id to a plain dict of ConnectorProduct
    fields. `errors` optionally maps an external_id to an exception that
    `get_product` raises instead of returning data, to simulate a merchant
    failure.
    """

    def __init__(
        self,
        products: Mapping[str, Mapping[str, object]] | None = None,
        errors: Mapping[str, Exception] | None = None,
    ) -> None:
        self._products: dict[str, ConnectorProduct] = {
            external_id: _build_product(external_id, data)
            for external_id, data in (products or {}).items()
        }
        self._errors: dict[str, Exception] = dict(errors or {})

    def get_product(self, external_id: str) -> ConnectorProduct:
        if external_id in self._errors:
            raise self._errors[external_id]
        try:
            return self._products[external_id]
        except KeyError:
            raise ProductNotFoundError(
                f"FakeStoreConnector has no product {external_id!r}"
            ) from None

    def update_product(self, external_id: str, **changes: object) -> None:
        """Simulate a merchant-side change (price, stock, seller, ...)."""
        if external_id not in self._products:
            raise ProductNotFoundError(f"FakeStoreConnector has no product {external_id!r}")
        self._products[external_id] = dataclasses.replace(self._products[external_id], **changes)
