"""Sitemap-based discovery — the "search_products(query)" equivalent for
a retailer with no public catalog-search API (Cultura, JouéClub,
E.Leclerc, La Grande Récré all confirmed to have none this session — see
connectors/defaults.py). Every one of them DOES publish a standard
sitemap.xml/sitemap-index.xml (the sitemaps.org protocol — a public,
universally-documented SEO convention, not a private/undocumented
endpoint), enumerating essentially their entire product catalog. That is
enough to detect "a page was just created for this product" ahead of a
drop: a keyword match against a sitemap URL that wasn't there on a
previous crawl is exactly the CATALOG_SEARCH signal this project needs,
without ever calling an external search engine or reverse-engineering a
private API.

Two-phase, matching this project's established "concurrent network,
sequential everything else" discipline:
  1. Fetch + cache the retailer's product-sitemap URL list (one real
     network round-trip per cache refresh, not per search query — several
     Products searching the same retailer in one discovery cycle reuse
     the same in-memory list).
  2. For URLs whose slug contains every one of the query's own
     significant words, fetch the real product page through the
     retailer's existing connector (GenericSchemaOrgConnector) to get
     real price/stock/EAN — a sitemap entry alone is just a URL, never
     treated as a "found" product until the actual page confirms it.

Bounded on purpose: a retailer's sitemap can run into the hundreds of
thousands of URLs (JouéClub's single product sitemap file alone was
40,000+ during this session's research) — _MAX_SITEMAP_FILES and
_MAX_URLS_PER_SOURCE cap how much of it is ever held in memory or
scanned, and _MAX_PRODUCT_PAGE_FETCHES caps how many full product pages
one search() call will fetch, so a broad query can never turn into a
scraping run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from xml.etree import ElementTree

import httpx

from connectors.base import ConnectorError, ProductNotFoundError
from discovery.base import DiscoveryError, DiscoveryUnavailableError, RetailDiscoverySource
from discovery.product_attributes import normalize

if TYPE_CHECKING:
    from connectors.base import BaseConnector, ConnectorProduct

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USER_AGENT = (
    "RetailOpportunityAssistant/0.1 (+public sitemap catalog discovery; no auto-purchase)"
)
DEFAULT_CACHE_TTL_SECONDS = 3600.0  # re-crawl at most once an hour per retailer

_MAX_SITEMAP_FILES = 25
_MAX_URLS_PER_SOURCE = 300_000
_MAX_PRODUCT_PAGE_FETCHES = 5

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


class SitemapDiscoverySource(RetailDiscoverySource):
    def __init__(
        self,
        *,
        sitemap_index_url: str,
        connector: BaseConnector,  # a GenericSchemaOrgConnector in every real use so far
        merchant_name: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._sitemap_index_url = sitemap_index_url
        self._connector = connector
        self._merchant_name = merchant_name
        self._cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self._client = httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )
        self._cached_urls: list[str] | None = None
        self._cached_at: datetime | None = None

    def search(
        self, query: str, *, ean: str | None = None, mpn: str | None = None, limit: int = 10
    ) -> list[ConnectorProduct]:
        query_words = set(normalize(query).split())
        if not query_words:
            return []

        try:
            urls = self._get_catalog_urls()
        except DiscoveryError:
            raise
        except Exception as exc:  # noqa: BLE001 - a crawl failure is a discovery failure, not a crash
            raise DiscoveryError(f"{self._merchant_name}: sitemap crawl failed: {exc}") from exc

        matches = [url for url in urls if _slug_matches(url, query_words)][
            :_MAX_PRODUCT_PAGE_FETCHES
        ]
        if not matches:
            return []

        products: list[ConnectorProduct] = []
        for url in matches[:limit]:
            external_id = _path_of(url)
            try:
                products.append(self._connector.get_product(external_id))
            except ProductNotFoundError:
                continue  # sitemap entry is stale (page removed since the crawl) — not an error
            except ConnectorError as exc:
                logger.warning(
                    "%s: sitemap match %r could not be fetched: %s", self._merchant_name, url, exc
                )
                continue
        return products

    def _get_catalog_urls(self) -> list[str]:
        now = datetime.now(UTC)
        if (
            self._cached_urls is not None
            and self._cached_at is not None
            and now - self._cached_at < self._cache_ttl
        ):
            return self._cached_urls

        leaf_sitemap_urls = self._resolve_leaf_sitemaps(self._sitemap_index_url)
        urls: list[str] = []
        for sitemap_url in leaf_sitemap_urls[:_MAX_SITEMAP_FILES]:
            urls.extend(self._fetch_urlset(sitemap_url))
            if len(urls) >= _MAX_URLS_PER_SOURCE:
                break

        self._cached_urls = urls[:_MAX_URLS_PER_SOURCE]
        self._cached_at = now
        logger.info(
            "%s: sitemap crawl cached %d product URL(s) from %d file(s)",
            self._merchant_name,
            len(self._cached_urls),
            min(len(leaf_sitemap_urls), _MAX_SITEMAP_FILES),
        )
        return self._cached_urls

    def _resolve_leaf_sitemaps(self, index_url: str) -> list[str]:
        """A sitemap index nests one level of <sitemap><loc> entries
        pointing at the real <urlset> files; a retailer that publishes a
        single flat sitemap (no index) is also handled — its own <url>
        entries are returned as if it were the sole leaf."""
        root = self._fetch_xml(index_url)
        tag = root.tag.removeprefix(_SITEMAP_NS)
        if tag == "sitemapindex":
            return [
                loc.text.strip()
                for sitemap in root.findall(f"{_SITEMAP_NS}sitemap")
                if (loc := sitemap.find(f"{_SITEMAP_NS}loc")) is not None and loc.text
            ]
        return [index_url]  # already a flat urlset

    def _fetch_urlset(self, sitemap_url: str) -> list[str]:
        root = self._fetch_xml(sitemap_url)
        return [
            loc.text.strip()
            for url_elem in root.findall(f"{_SITEMAP_NS}url")
            if (loc := url_elem.find(f"{_SITEMAP_NS}loc")) is not None and loc.text
        ]

    def _fetch_xml(self, url: str) -> ElementTree.Element:
        try:
            response = self._client.get(url)
        except httpx.TimeoutException as exc:
            raise DiscoveryError(f"{self._merchant_name}: timeout fetching sitemap {url}") from exc
        except httpx.RequestError as exc:
            raise DiscoveryError(
                f"{self._merchant_name}: network error fetching sitemap {url}: {exc}"
            ) from exc

        if response.status_code == 404:
            raise DiscoveryUnavailableError(f"{self._merchant_name}: sitemap not found at {url}")
        if response.status_code >= 400:
            raise DiscoveryError(
                f"{self._merchant_name}: sitemap fetch returned HTTP {response.status_code}"
            )
        try:
            return ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise DiscoveryError(
                f"{self._merchant_name}: sitemap at {url} is not valid XML"
            ) from exc


def _path_of(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[-1]


def _slug_matches(url: str, query_words: set[str]) -> bool:
    slug_words = set(normalize(_path_of(url).replace("-", " ").replace("_", " ")).split())
    return query_words.issubset(slug_words)
