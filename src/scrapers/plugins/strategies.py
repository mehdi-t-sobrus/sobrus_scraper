"""
src/scrapers/plugins/strategies.py
====================================
Concrete URL discovery strategy plugins.

Each class handles one discovery pattern used across the 20+ target sites.
The ``PLUGIN_REGISTRY`` in ``discoverer.py`` maps a ``domain`` string to the
correct plugin class; the discoverer instantiates it with a ``DiscoveryContext``
and calls ``plugin.discover()``.

Strategies implemented here
----------------------------
SitemapXMLPlugin
    Parses one or more ``<urlset>`` / ``<sitemapindex>`` XML sitemaps.
    Handles nested sitemap indexes recursively.
    Filters URLs using ``SiteConfig.product_url_patterns``.

CategoryCrawlPlugin
    Starts from a list of category page URLs, follows pagination via a
    CSS selector, and harvests product links from each listing page.
    Uses ``selectolax`` for fast HTML parsing (never BeautifulSoup4).

JsonLdApiPlugin
    Some sites expose a REST/JSON endpoint (or embed JSON-LD on category
    pages) that lists all products with their canonical URLs.
    Iterates paginated API responses until exhausted.

HybridSitemapCategoryPlugin
    Combines sitemap discovery with a category-page fallback:
    reads the sitemap for speed, then fills gaps by crawling categories
    for any path prefix not found in the sitemap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncIterator
from xml.etree import ElementTree as ET

from curl_cffi.requests import AsyncSession, RequestsError
from selectolax.parser import HTMLParser

from .base import BaseDiscoveryPlugin, DiscoveryContext

logger = logging.getLogger(__name__)

# XML namespace used in sitemap <urlset> and <sitemapindex> elements
_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# ---------------------------------------------------------------------------
# Strategy 1: XML Sitemap (sitemap.xml / sitemap_index.xml)
# ---------------------------------------------------------------------------

class SitemapXMLPlugin(BaseDiscoveryPlugin):
    """
    Discovers product URLs by parsing XML sitemaps.

    Supports:
    - Flat ``<urlset>`` sitemaps (list of <loc> entries)
    - ``<sitemapindex>`` (recursive — fetches each child sitemap)
    - Gzip-compressed sitemaps (.xml.gz) — curl_cffi decompresses automatically
    - ``lastmod`` date filtering via ``min_lastmod`` in ``extra_config``

    Configuration in SiteConfig
    ---------------------------
    ``sitemap_url``         : Root sitemap URL (required)
    ``product_url_patterns``: Regex filters applied to each <loc> value

    Example SiteConfig.product_url_patterns
    ----------------------------------------
    ``["/products/", "/p/\\d+"]``
    """

    def __init__(self, ctx: DiscoveryContext, min_lastmod: str | None = None) -> None:
        super().__init__(ctx)
        self._min_lastmod = min_lastmod  # e.g. "2024-01-01" — skip older URLs

    async def discover(self) -> AsyncIterator[str]:
        """Yield product URLs extracted from the configured sitemap."""
        if not self.ctx.sitemap_url:
            self.logger.error(
                "SitemapXMLPlugin requires sitemap_url in SiteConfig for %s",
                self.ctx.domain,
            )
            return

        async for url in self._process_sitemap(self.ctx.sitemap_url):
            yield url

    async def _process_sitemap(self, sitemap_url: str) -> AsyncIterator[str]:
        """Fetch and parse a single sitemap URL; recurse into sitemap indexes."""
        self.logger.info("Fetching sitemap: %s", sitemap_url)
        try:
            async with AsyncSession(impersonate=self.ctx.impersonate_profile) as session:
                resp = await session.get(
                    sitemap_url,
                    proxies=self._proxies(),
                    timeout=30,
                    headers={"Accept-Encoding": "gzip, deflate, br"},
                )
            resp.raise_for_status()
            xml_text = resp.text
        except (RequestsError, Exception) as exc:
            self.logger.error("Failed to fetch sitemap %s: %s", sitemap_url, exc)
            return

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            self.logger.error("XML parse error on %s: %s", sitemap_url, exc)
            return

        # Strip namespace for easier tag matching
        tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        if tag == "sitemapindex":
            # Recurse: fetch each child sitemap
            child_locs = [
                el.text.strip()
                for el in root.findall("sm:sitemap/sm:loc", _SITEMAP_NS)
                if el.text
            ]
            self.logger.info(
                "Sitemap index at %s — %d child sitemaps found.",
                sitemap_url, len(child_locs),
            )
            for child_url in child_locs:
                await self._delay()
                async for url in self._process_sitemap(child_url):
                    yield url

        elif tag == "urlset":
            count = 0
            for url_el in root.findall("sm:url", _SITEMAP_NS):
                loc_el = url_el.find("sm:loc", _SITEMAP_NS)
                if loc_el is None or not loc_el.text:
                    continue

                loc = loc_el.text.strip()

                # Optional: skip entries older than min_lastmod
                if self._min_lastmod:
                    lastmod_el = url_el.find("sm:lastmod", _SITEMAP_NS)
                    if lastmod_el is not None and lastmod_el.text:
                        if lastmod_el.text.strip() < self._min_lastmod:
                            continue

                if self._matches_product_pattern(loc):
                    count += 1
                    yield loc

            self.logger.info(
                "Sitemap %s → %d product URLs extracted.", sitemap_url, count
            )
        else:
            self.logger.warning(
                "Unknown sitemap root tag <%s> at %s", tag, sitemap_url
            )


# ---------------------------------------------------------------------------
# Strategy 2: Category page crawl (CSS selectors + pagination)
# ---------------------------------------------------------------------------

class CategoryCrawlPlugin(BaseDiscoveryPlugin):
    """
    Discovers product URLs by crawling category / listing pages.

    Pagination is followed automatically using a CSS selector that points
    to the "Next page" link.  The crawl stops when no next link is found
    or when ``max_pages`` is reached.

    Configuration fields (passed via ``extra_config`` dict at init)
    ---------------------------------------------------------------
    ``category_urls``       : list[str] — seed category page URLs
    ``product_link_selector``: CSS selector for product links on a listing page
                               e.g. ``"a.product-tile__link"``
    ``next_page_selector``  : CSS selector for the pagination next-link
                               e.g. ``"a.pagination__next"``
    ``max_pages``           : int — safety ceiling per category (default 200)
    ``url_prefix``          : str — prepended to relative hrefs (e.g. "https://example.com")

    Uses ``selectolax`` (NOT BeautifulSoup4 — see CLAUDE.md §2).
    """

    def __init__(self, ctx: DiscoveryContext, extra_config: dict) -> None:
        super().__init__(ctx)
        self._category_urls: list[str] = extra_config.get("category_urls", [])
        self._product_sel: str = extra_config.get(
            "product_link_selector", "a[href*='/product']"
        )
        self._next_sel: str = extra_config.get("next_page_selector", "a.next")
        self._max_pages: int = int(extra_config.get("max_pages", 200))
        self._url_prefix: str = extra_config.get("url_prefix", "").rstrip("/")

    async def discover(self) -> AsyncIterator[str]:
        """Yield product URLs from all configured category pages."""
        seen: set[str] = set()
        sem = asyncio.Semaphore(self.ctx.max_concurrency)

        async def _crawl_category(seed_url: str) -> list[str]:
            """Crawl a single category, following pagination."""
            found: list[str] = []
            current_url: str | None = seed_url
            page = 0

            async with AsyncSession(impersonate=self.ctx.impersonate_profile) as session:
                while current_url and page < self._max_pages:
                    page += 1
                    self.logger.debug("Category crawl [p%d]: %s", page, current_url)
                    try:
                        async with sem:
                            resp = await session.get(
                                current_url,
                                proxies=self._proxies(),
                                timeout=30,
                            )
                        resp.raise_for_status()
                    except (RequestsError, Exception) as exc:
                        self.logger.warning(
                            "Category page fetch failed %s: %s", current_url, exc
                        )
                        break

                    tree = HTMLParser(resp.text)

                    # Extract product links
                    for node in tree.css(self._product_sel):
                        href = node.attributes.get("href", "")
                        if not href:
                            continue
                        full_url = (
                            href if href.startswith("http")
                            else f"{self._url_prefix}{href}"
                        )
                        if self._matches_product_pattern(full_url):
                            found.append(full_url)

                    # Follow next page
                    next_node = tree.css_first(self._next_sel)
                    if next_node:
                        next_href = next_node.attributes.get("href", "")
                        current_url = (
                            next_href if next_href.startswith("http")
                            else f"{self._url_prefix}{next_href}"
                        ) if next_href else None
                    else:
                        current_url = None

                    await self._delay()

            self.logger.info(
                "Category %s → %d URLs across %d pages.", seed_url, len(found), page
            )
            return found

        # Run all category seeds concurrently (bounded by sem inside _crawl_category)
        tasks = [_crawl_category(cat) for cat in self._category_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                self.logger.error("Category crawl task failed: %s", result)
                continue
            for url in result:
                if url not in seen:
                    seen.add(url)
                    yield url


# ---------------------------------------------------------------------------
# Strategy 3: JSON-LD embedded in category pages / REST API endpoints
# ---------------------------------------------------------------------------

class JsonLdApiPlugin(BaseDiscoveryPlugin):
    """
    Discovers product URLs from sites that expose structured data.

    Two sub-modes:
    A) ``api_mode=True``  — calls a paginated REST endpoint that returns JSON
       with a list of products.  Reads ``url_field`` from each item.
    B) ``api_mode=False`` — fetches category pages and extracts ``application/ld+json``
       blocks, looking for ``@type: Product`` entries with a ``url`` field.

    Configuration (via ``extra_config``)
    -------------------------------------
    ``api_mode``        : bool — True = REST JSON API, False = JSON-LD extraction
    ``api_endpoint``    : str — base API URL (mode A)
    ``page_param``      : str — query param for page number (default "page")
    ``page_size_param`` : str — query param for page size (default "pageSize")
    ``page_size``       : int — items per page (default 48)
    ``url_field``       : str — dot-path to the URL in each JSON item
                          e.g. "url" or "links.self" (default "url")
    ``category_urls``   : list[str] — category pages to scrape for JSON-LD (mode B)
    ``max_pages``       : int — page ceiling (default 500)
    """

    def __init__(self, ctx: DiscoveryContext, extra_config: dict) -> None:
        super().__init__(ctx)
        self._api_mode: bool = extra_config.get("api_mode", False)
        self._api_endpoint: str = extra_config.get("api_endpoint", "")
        self._page_param: str = extra_config.get("page_param", "page")
        self._page_size_param: str = extra_config.get("page_size_param", "pageSize")
        self._page_size: int = int(extra_config.get("page_size", 48))
        self._url_field: str = extra_config.get("url_field", "url")
        self._category_urls: list[str] = extra_config.get("category_urls", [])
        self._max_pages: int = int(extra_config.get("max_pages", 500))

    async def discover(self) -> AsyncIterator[str]:
        if self._api_mode:
            async for url in self._discover_via_api():
                yield url
        else:
            async for url in self._discover_via_jsonld():
                yield url

    async def _discover_via_api(self) -> AsyncIterator[str]:
        """Paginate through a JSON REST API yielding product URLs."""
        page = 1
        total_found = 0

        async with AsyncSession(impersonate=self.ctx.impersonate_profile) as session:
            while page <= self._max_pages:
                params = {
                    self._page_param: page,
                    self._page_size_param: self._page_size,
                }
                try:
                    resp = await session.get(
                        self._api_endpoint,
                        params=params,
                        proxies=self._proxies(),
                        timeout=30,
                        headers={"Accept": "application/json"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    self.logger.error("API page %d failed: %s", page, exc)
                    break

                items = self._extract_items(data)
                if not items:
                    self.logger.info("API exhausted at page %d.", page)
                    break

                for item in items:
                    url = self._resolve_field(item, self._url_field)
                    if url and self._matches_product_pattern(url):
                        total_found += 1
                        yield url

                page += 1
                await self._delay()

        self.logger.info(
            "JSON API %s → %d product URLs extracted.", self._api_endpoint, total_found
        )

    async def _discover_via_jsonld(self) -> AsyncIterator[str]:
        """Extract JSON-LD Product entries embedded in category page HTML."""
        seen: set[str] = set()

        async with AsyncSession(impersonate=self.ctx.impersonate_profile) as session:
            for cat_url in self._category_urls:
                try:
                    resp = await session.get(
                        cat_url, proxies=self._proxies(), timeout=30
                    )
                    resp.raise_for_status()
                except Exception as exc:
                    self.logger.warning("JSON-LD page %s failed: %s", cat_url, exc)
                    await self._delay()
                    continue

                tree = HTMLParser(resp.text)
                for script in tree.css("script[type='application/ld+json']"):
                    raw = script.text(strip=True)
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # Handle @graph arrays and single objects
                    entities = (
                        payload.get("@graph", [payload])
                        if isinstance(payload, dict)
                        else payload if isinstance(payload, list)
                        else []
                    )
                    for entity in entities:
                        if not isinstance(entity, dict):
                            continue
                        etype = entity.get("@type", "")
                        if etype not in ("Product", "ItemList"):
                            continue
                        url = entity.get("url") or entity.get("@id", "")
                        if url and url not in seen and self._matches_product_pattern(url):
                            seen.add(url)
                            yield url

                await self._delay()

    @staticmethod
    def _extract_items(data: dict | list) -> list:
        """Extract item list from various API response shapes."""
        if isinstance(data, list):
            return data
        for key in ("products", "items", "results", "data", "hits"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return []

    @staticmethod
    def _resolve_field(item: dict, field_path: str) -> str | None:
        """Resolve a dot-notation field path from a dict, e.g. 'links.canonical'."""
        parts = field_path.split(".")
        node = item
        for part in parts:
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return str(node) if node else None


# ---------------------------------------------------------------------------
# Strategy 4: Hybrid — Sitemap + Category fallback
# ---------------------------------------------------------------------------

class HybridSitemapCategoryPlugin(BaseDiscoveryPlugin):
    """
    Combines sitemap-based discovery with category-page crawling as fallback.

    Useful for sites where the sitemap covers most products but misses
    recently added items that haven't been re-indexed yet.

    Algorithm
    ---------
    1. Run ``SitemapXMLPlugin`` — collect all URLs.
    2. For each category seed in ``category_urls``, crawl listing pages.
    3. Yield any URL from step 2 not already found in step 1.

    Configuration (via ``extra_config``)
    -------------------------------------
    All fields from both ``SitemapXMLPlugin`` and ``CategoryCrawlPlugin``.
    """

    def __init__(self, ctx: DiscoveryContext, extra_config: dict) -> None:
        super().__init__(ctx)
        self._sitemap_plugin = SitemapXMLPlugin(ctx)
        self._category_plugin = CategoryCrawlPlugin(ctx, extra_config)

    async def discover(self) -> AsyncIterator[str]:
        """Yield sitemap URLs first, then new URLs found via category crawl."""
        sitemap_urls: set[str] = set()

        # Phase 1: sitemap
        async for url in self._sitemap_plugin.discover():
            sitemap_urls.add(url)
            yield url

        self.logger.info(
            "Hybrid [%s]: %d URLs from sitemap. Starting category gap-fill.",
            self.ctx.domain, len(sitemap_urls),
        )

        # Phase 2: category crawl — only yield what sitemap missed
        gap_count = 0
        async for url in self._category_plugin.discover():
            if url not in sitemap_urls:
                gap_count += 1
                sitemap_urls.add(url)
                yield url

        self.logger.info(
            "Hybrid [%s]: %d additional URLs from category crawl.",
            self.ctx.domain, gap_count,
        )
