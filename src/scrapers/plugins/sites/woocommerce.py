"""
src/scrapers/plugins/sites/woocommerce.py
==========================================
Generic discovery plugin for any WooCommerce store using Yoast SEO sitemaps.

WooCommerce + Yoast sitemap structure
---------------------------------------
/sitemap_index.xml  →  sitemapindex listing child sitemaps:
    /product-sitemap.xml          ← ALL product URLs — this is what we want
    /product-sitemap1.xml         ← paginated if >1000 products (Yoast splits at 1000)
    /product-sitemap2.xml
    ...
    /post-sitemap.xml
    /page-sitemap.xml
    /category-sitemap.xml
    /product_cat-sitemap.xml
    /product_tag-sitemap.xml

Strategy: read the sitemap index, find all child sitemaps whose name starts with
"product-sitemap", fetch each one, yield every URL.

WooCommerce product URL pattern
---------------------------------
WooCommerce uses clean slugs directly off the root by default:
    https://store.com/<product-slug>/

Unlike Shopify there is NO /products/ prefix. This is why we rely entirely on
reading only the product-sitemap child files rather than URL pattern matching —
there's no reliable way to distinguish a product URL from a category or page
URL by path alone.

WooCommerce JSON-LD structure (Yoast-enhanced, standard across stores)
------------------------------------------------------------------------
{
  "@context": "https://schema.org/",
  "@type": "Product",
  "name": "Eucerin Anti-Pigment Cleansing Gel 200ml",
  "image": "https://cotepara.ma/wp-content/uploads/...",
  "description": "...",
  "sku": "EU-APG-200",
  "brand": {"@type": "Brand", "name": "Eucerin"},
  "offers": {
    "@type": "Offer",
    "url": "https://cotepara.ma/eucerin-anti-pigment-cleansing-gel-200ml/",
    "priceCurrency": "MAD",
    "price": "199.00",
    "availability": "https://schema.org/InStock"
  }
}

Note: some WooCommerce stores use a @graph array (Yoast schema graph format).
The existing _extract_json_ld() in silver_products.py handles both.

Adding a new WooCommerce/Yoast site
-------------------------------------
1. Add SiteConfig in Django Admin with the site's domain.
2. Add one line to PLUGIN_REGISTRY in discoverer.py:
       "yourstore.com": (WooCommercePlugin, {}),
3. Add CSS selectors to _CSS_SELECTOR_REGISTRY in silver_products.py.
That's it — no new plugin code needed.
"""

from __future__ import annotations

import asyncio
import gzip
import re
from typing import AsyncIterator
from xml.etree import ElementTree as ET

from curl_cffi.requests import AsyncSession, RequestsError

from scrapers.plugins.base import BaseDiscoveryPlugin

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Matches Yoast product sitemap child URLs.
# Examples:
#   https://store.com/product-sitemap.xml
#   https://store.com/product-sitemap1.xml
#   https://store.com/product-sitemap2.xml
_PRODUCT_SITEMAP_RE = re.compile(
    r"/product-sitemap\d*\.xml$"
)


class WooCommercePlugin(BaseDiscoveryPlugin):
    """
    Generic discovery plugin for any WooCommerce + Yoast SEO store.

    Reads /sitemap_index.xml, finds all product-sitemap*.xml child sitemaps,
    fetches each one, and yields every product URL.

    Reusable — register any WooCommerce domain in PLUGIN_REGISTRY with this
    class and no extra config needed.
    """

    async def discover(self) -> AsyncIterator[str]:
        """Yield all product URLs from the WooCommerce/Yoast sitemap index."""
        index_url = f"https://{self.ctx.domain}/sitemap_index.xml"
        self.logger.info(
            "[woocommerce:%s] Fetching sitemap index: %s",
            self.ctx.domain, index_url,
        )

        async with AsyncSession(impersonate=self.ctx.impersonate_profile) as session:

            # ------------------------------------------------------------------
            # Step 1 — Fetch sitemap index and extract product sitemap URLs
            # ------------------------------------------------------------------
            product_sitemap_urls = await self._get_product_sitemap_urls(
                session, index_url
            )

            if not product_sitemap_urls:
                self.logger.error(
                    "[woocommerce:%s] No product sitemaps found in index.",
                    self.ctx.domain,
                )
                return

            self.logger.info(
                "[woocommerce:%s] Found %d product sitemap(s).",
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
                    "[woocommerce:%s] Fetching: %s",
                    self.ctx.domain, sitemap_url,
                )

                try:
                    xml_text = await self._fetch_xml(session, sitemap_url)
                except Exception as exc:
                    self.logger.error(
                        "[woocommerce:%s] Failed to fetch %s: %s",
                        self.ctx.domain, sitemap_url, exc,
                    )
                    continue

                if not xml_text:
                    continue

                try:
                    root = ET.fromstring(xml_text)
                except ET.ParseError as exc:
                    self.logger.error(
                        "[woocommerce:%s] XML parse error in %s: %s",
                        self.ctx.domain, sitemap_url, exc,
                    )
                    continue

                for url_el in root.findall("sm:url", _SITEMAP_NS):
                    loc_el = url_el.find("sm:loc", _SITEMAP_NS)
                    if loc_el is None or not loc_el.text:
                        continue
                    loc = loc_el.text.strip()
                    if loc:
                        page_count += 1
                        total += 1
                        yield loc

                self.logger.info(
                    "[woocommerce:%s] %s → %d product URLs (total: %d)",
                    self.ctx.domain,
                    sitemap_url.split("/")[-1],
                    page_count,
                    total,
                )

        self.logger.info(
            "[woocommerce:%s] Discovery complete — %d product URLs total.",
            self.ctx.domain, total,
        )

    async def _get_product_sitemap_urls(
        self,
        session: AsyncSession,
        index_url: str,
    ) -> list[str]:
        """
        Fetch the sitemap index and return only product-sitemap*.xml URLs.
        """
        try:
            xml_text = await self._fetch_xml(session, index_url)
        except Exception as exc:
            self.logger.error(
                "[woocommerce:%s] Sitemap index fetch failed: %s",
                self.ctx.domain, exc,
            )
            return []

        if not xml_text:
            return []

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            self.logger.error(
                "[woocommerce:%s] Sitemap index parse error: %s",
                self.ctx.domain, exc,
            )
            return []

        urls: list[str] = []
        for sitemap_el in root.findall("sm:sitemap", _SITEMAP_NS):
            loc_el = sitemap_el.find("sm:loc", _SITEMAP_NS)
            if loc_el is None or not loc_el.text:
                continue
            loc = loc_el.text.strip()
            if _PRODUCT_SITEMAP_RE.search(loc):
                urls.append(loc)

        return urls

    async def _fetch_xml(
        self,
        session: AsyncSession,
        url: str,
    ) -> str | None:
        """
        Fetch a URL and return the body as a decoded string.
        Handles gzip-compressed responses transparently.
        Sanitises illegal XML control characters.
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
            self.logger.warning("[woocommerce:%s] 404 for %s", self.ctx.domain, url)
            return None

        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} for {url}")

        raw: bytes = resp.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)

        xml_text = raw.decode("utf-8", errors="replace")
        xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", xml_text)
        return xml_text
