"""Common connector abstraction — no HTTP, no scraping, no merchant specifics.

Every merchant connector (fake or real) implements `BaseConnector` and
returns a `ConnectorProduct`. The rest of the system depends only on this
module, never on a merchant's actual site structure.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


class ConnectorError(Exception):
    """Generic connector-level failure (merchant unreachable, bad response, ...)."""


class ProductNotFoundError(ConnectorError):
    """Raised when the connector has no product for the given external_id."""


@dataclass(frozen=True, slots=True)
class ConnectorProduct:
    """A single product snapshot as reported by one merchant, right now.

    This is raw observed data, not a validated domain object — a connector
    mirrors what the merchant actually returns, including nonsensical values.
    Validation belongs to later stages (matcher, decision engine).

    `price` is always coerced to `Decimal` (via `str()` first, never straight
    from a float) so binary float artifacts never enter the system, no
    matter which type a connector implementation happens to pass in.
    """

    external_id: str
    name: str
    price: Decimal
    currency: str
    available: bool
    seller: str
    url: str
    ean: str | None = None
    mpn: str | None = None
    image_url: str | None = None
    # Which channel the stock is actually in (web / store pickup /
    # partner). Retailers routinely differ per channel — a product can
    # be orderable in a shop while the site says sold out — and the
    # plain `available` flag cannot express that. None = the merchant
    # exposes no channel breakdown.
    availability_detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.price, Decimal):
            object.__setattr__(self, "price", Decimal(str(self.price)))


class BaseConnector(ABC):
    """Interface every merchant connector must implement."""

    @abstractmethod
    def get_product(self, external_id: str) -> ConnectorProduct:
        """Fetch one product's current data from the merchant.

        Raises:
            ProductNotFoundError: external_id is unknown to this merchant.
            ConnectorError: any other connector-level failure.
        """
        raise NotImplementedError
