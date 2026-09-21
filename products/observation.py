"""ProductObservation — the canonical, validated snapshot our system trusts.

A ConnectorProduct is raw transport data straight from one merchant; a
ProductObservation is what the rest of the system (matcher, monitoring,
decision engine) actually reads. The monitoring engine never needs to know
a specific merchant's internal structure — only this normalized shape.

Not coupled to SQLAlchemy: this is the business model for a single
observation, not (yet) its historical storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from connectors.base import ConnectorProduct


@dataclass(frozen=True, slots=True)
class ProductObservation:
    """A validated, timestamped product snapshot from a named merchant.

    Unlike ConnectorProduct, this represents data accepted into our system:
    construction raises ValueError on missing required fields, a negative
    price, an empty currency, or a naive (non timezone-aware) observed_at.
    """

    merchant: str
    external_id: str
    name: str
    price: Decimal
    currency: str
    available: bool
    url: str
    observed_at: datetime
    ean: str | None = None
    mpn: str | None = None
    seller: str | None = None
    image_url: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty("merchant", self.merchant)
        _require_non_empty("external_id", self.external_id)
        _require_non_empty("name", self.name)
        _require_non_empty("url", self.url)

        if self.price < 0:
            raise ValueError("price must not be negative")

        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))

        normalized_currency = self.currency.strip().upper()
        if not normalized_currency:
            raise ValueError("currency must not be empty")
        object.__setattr__(self, "currency", normalized_currency)

    @classmethod
    def from_connector_product(
        cls,
        product: ConnectorProduct,
        *,
        merchant: str,
        observed_at: datetime | None = None,
    ) -> ProductObservation:
        """Normalize a connector's raw result into a trusted observation."""
        return cls(
            merchant=merchant,
            external_id=product.external_id,
            name=product.name,
            price=product.price,
            currency=product.currency,
            available=product.available,
            url=product.url,
            observed_at=observed_at or datetime.now(UTC),
            ean=product.ean,
            mpn=product.mpn,
            seller=product.seller,
            image_url=product.image_url,
        )


def _require_non_empty(field_name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
