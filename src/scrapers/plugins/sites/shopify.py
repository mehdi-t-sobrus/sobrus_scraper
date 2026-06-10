"""
src/scrapers/plugins/sites/shopify.py
======================================
Generic discovery plugin for any Shopify store.

Shopify sitemap structure (identical across all stores)
--------------------------------------------------------
/sitemap.xml  →  sitemapindex listing child sitemaps:
    /sitemap_products_1.xml?from=<id>&to=<id>
    /sitemap_products_2.xml?from=<id>&to=<id>
    ...
    /sitemap_collections_1.xml?...
    /sitemap_pages_1.xml?...
    /sitemap_blogs_1.xml
    /ar/sitemap_products_*.xml  (duplicate Arabic versions — skipped)

We only fetch sitemap_products_*.xml files at the default locale (no /ar/ prefix).
The ?from= and ?to= query params are pagination tokens — they must be preserved
exactly as they appear in the sitemap index.

Shopify JSON-LD structure (consistent across all stores)
---------------------------------------------------------
{
  "@context": "http://schema.org/",
  "@type": "Product",
  "name": "...",
  "description": "...",
  "url": "https://store.com/products/handle",
  "image": ["https://cdn.shopify.com/..."],
  "brand": {"@type": "Brand", "name": "..."},
  "offers": [{
    "@type": "Offer",
    "price": "199.00",
    "priceCurrency": "MAD",
    "availability": "http://schema.org/InStock",
    "sku": "..."
  }]
}

Adding a new Shopify site
--------------------------
1. Add a SiteConfig in Django Admin with domain = "yourstore.com"
2. Add one line to PLUGIN_REGISTRY in discoverer.py:
       "yourstore.com": (ShopifyPlugin, {}),
3. Add CSS selectors to _CSS_SELECTOR_REGISTRY in silver_products.py
   (or rely on the generic Shopify selectors already registered there)
That's it — no new plugin code needed.
"""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator
from xml.etree import ElementTree as ET

from curl_cffi.requests import AsyncSession, RequestsError

from scrapers.plugins.base import BaseDiscoveryPlugin

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Matches Shopify product sitemaps at the default locale only.
# Excludes /ar/, /fr/, /en/ prefixed duplicates.
_PRODUCTS_SITEMAP_RE = re.compile(
    r"^https?://[^/]+/sitemap_products_\d+\.xml"
)

# Matches Shopify product URLs: /products/<handle>
_PRODUCT_URL_RE = re.compile(r"/products/[^/?#]+$")


class ShopifyPlugin(BaseDiscoveryPlugin):
    """
    Generic discovery plugin for any Shopify store.

    Reads /sitemap.xml, finds all sitemap_products_*.xml child sitemaps
    at the default locale, fetches each one, and yields every product URL.

    Reusable — register any Shopify domain in PLUGIN_REGISTRY with this class
    and no extra config needed.
    """

    async def discover(self) -> AsyncIterator[str]:
        """Yield all product URLs from the Shopify sitemap index."""
        sitemap_index_url = f"https://{self.ctx.domain}/sitemap.xml"
        self.logger.info(
            "[shopify:%s] Fetching sitemap index: %s",
            self.ctx.domain, sitemap_index_url,
        )

        async with AsyncSession(impersonate=self.ctx.impersonate_profile) as session:
            # ------------------------------------------------------------------
            # Step 1 — Fetch and parse the sitemap index
            # ------------------------------------------------------------------
            product_sitemap_urls = await self._fetch_product_sitemap_urls(
                session, sitemap_index_url
            )

            if not product_sitemap_urls:
                self.logger.error(
                    "[shopify:%s] No product sitemaps found in index.", self.ctx.domain
                )
                return

            self.logger.info(
                "[shopify:%s] Found %d product sitemap(s).",
                self.ctx.domain, len(product_sitemap_urls),
            )

            # ------------------------------------------------------------------
            # Step 2 — Fetch each product sitemap and yield URLs
            # ------------------------------------------------------------------
            total = 0
            for sitemap_url in product_sitemap_urls:
                await self._delay()
                page_count = 0

                self.logger.info(
                    "[shopify:%s] Fetching product sitemap: %s",
                    self.ctx.domain, sitemap_url,
                )

                try:
                    xml_text = await self._fetch_xml(session, sitemap_url)
                except Exception as exc:
                    self.logger.error(
                        "[shopify:%s] Failed to fetch %s: %s",
                        self.ctx.domain, sitemap_url, exc,
                    )
                    continue

                if not xml_text:
                    continue

                try:
                    root = ET.fromstring(xml_text)
                except ET.ParseError as exc:
                    self.logger.error(
                        "[shopify:%s] XML parse error in %s: %s",
                        self.ctx.domain, sitemap_url, exc,
                    )
                    continue

                for url_el in root.findall("sm:url", _SITEMAP_NS):
                    loc_el = url_el.find("sm:loc", _SITEMAP_NS)
                    if loc_el is None or not loc_el.text:
                        continue
                    loc = loc_el.text.strip()
                    if _PRODUCT_URL_RE.search(loc):
                        page_count += 1
                        total += 1
                        yield loc

                self.logger.info(
                    "[shopify:%s] %s → %d product URLs (total: %d)",
                    self.ctx.domain, sitemap_url.split("/")[-1].split("?")[0],
                    page_count, total,
                )

        self.logger.info(
            "[shopify:%s] Discovery complete — %d product URLs total.",
            self.ctx.domain, total,
        )

    async def _fetch_product_sitemap_urls(
        self,
        session: AsyncSession,
        index_url: str,
    ) -> list[str]:
        """
        Fetch the sitemap index and return only product sitemap URLs
        at the default locale (no language prefix).
        """
        try:
            xml_text = await self._fetch_xml(session, index_url)
        except Exception as exc:
            self.logger.error(
                "[shopify:%s] Sitemap index fetch failed: %s", self.ctx.domain, exc
            )
            return []

        if not xml_text:
            return []

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            self.logger.error(
                "[shopify:%s] Sitemap index parse error: %s", self.ctx.domain, exc
            )
            return []

        urls: list[str] = []
        for sitemap_el in root.findall("sm:sitemap", _SITEMAP_NS):
            loc_el = sitemap_el.find("sm:loc", _SITEMAP_NS)
            if loc_el is None or not loc_el.text:
                continue
            loc = loc_el.text.strip()
            # Only include default-locale product sitemaps
            if _PRODUCTS_SITEMAP_RE.match(loc):
                urls.append(loc)

        return urls

    async def _fetch_xml(
        self,
        session: AsyncSession,
        url: str,
    ) -> str | None:
        """
        Fetch a URL and return the response body as a string.
        Handles gzip-compressed responses transparently.
        """
        try:
            resp = await asyncio.wait_for(
                session.get(
                    url,
                    proxies=self._proxies(),
                    timeout=30,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Accept-Encoding": "identity",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                        "Upgrade-Insecure-Requests": "1",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                        "Sec-Fetch-User": "?1",
                    },
                ),
                timeout=35,
            )
        except (RequestsError, asyncio.TimeoutError) as exc:
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc

        if resp.status_code == 404:
            self.logger.warning("[shopify:%s] 404 for %s", self.ctx.domain, url)
            return None

        if resp.status_code != 200:
            raise RuntimeError(
                f"HTTP {resp.status_code} for {url}"
            )

        # Detect and decompress gzip by magic bytes
        import gzip as _gzip
        raw = resp.content
        if raw[:2] == b"\x1f\x8b":
            raw = _gzip.decompress(raw)

        # Decode and sanitise illegal XML control characters
        xml_text = raw.decode("utf-8", errors="replace")
        xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", xml_text)
        return xml_text
