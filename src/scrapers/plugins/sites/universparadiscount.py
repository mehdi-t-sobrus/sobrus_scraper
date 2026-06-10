"""
src/scrapers/plugins/sites/universparadiscount.py
==================================================
Scraper plugin for universparadiscount.ma

Site profile
------------
Platform  : PrestaShop
Discovery : Single gzip-compressed XML sitemap at /1_fr_0_sitemap.xml
            Contains ~11k mixed URLs (products, categories, CMS pages).
            Product URLs match: /<category-slug>/<digits>-<name>.html
Extraction: JSON-LD primary, CSS selectors fallback.
Currency  : MAD

JSON-LD structure observed
--------------------------
{
  "@type": "Product",
  "name": "...",
  "brand": {"@type": "Brand", "name": "..."},
  "offers": {"price": "355.41", "priceCurrency": "MAD", "availability": "..."}
}
No EAN/GTIN exposed — reference ID is the numeric segment in the URL slug.
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

# Matches: /<category-slug>/<digits>-<product-name>.html
_PRODUCT_RE = re.compile(r"/\d+-[^/]+\.html$")


class UniversparadiscountPlugin(BaseDiscoveryPlugin):
    """
    Discovery plugin for universparadiscount.ma.

    Fetches /1_fr_0_sitemap.xml (gzip-compressed), decompresses it,
    sanitises any malformed XML characters, then yields every URL
    matching the product URL pattern.
    """

    BASE_URL = "https://universparadiscount.ma"
    SITEMAP_PATTERN = "{base}/1_fr_{page}_sitemap.xml"
    MAX_SITEMAP_PAGES = 50

    async def discover(self) -> AsyncIterator[str]:
        """Yield all product URLs from the paginated PrestaShop sitemaps."""
        page = 0
        total = 0

        async with AsyncSession(impersonate=self.ctx.impersonate_profile) as session:
            while page < self.MAX_SITEMAP_PAGES:
                sitemap_url = self.SITEMAP_PATTERN.format(
                    base=self.BASE_URL, page=page
                )
                self.logger.info(
                    "[universparadiscount] Fetching sitemap page %d: %s",
                    page, sitemap_url,
                )

                # ----------------------------------------------------------
                # Fetch — request identity encoding so we get raw bytes
                # ----------------------------------------------------------
                try:
                    resp = await session.get(
                        sitemap_url,
                        proxies=self._proxies(),
                        timeout=30,
                        headers={"Accept-Encoding": "identity"},
                    )
                except (RequestsError, asyncio.TimeoutError) as exc:
                    self.logger.error(
                        "[universparadiscount] Sitemap page %d fetch failed: %s",
                        page, exc,
                    )
                    break

                if resp.status_code == 404:
                    self.logger.info(
                        "[universparadiscount] Sitemap page %d — 404, done.",
                        page,
                    )
                    break

                if resp.status_code != 200:
                    self.logger.warning(
                        "[universparadiscount] Sitemap page %d — HTTP %d, skipping.",
                        page, resp.status_code,
                    )
                    page += 1
                    await self._delay()
                    continue

                # ----------------------------------------------------------
                # Decompress — detect gzip by magic bytes regardless of headers
                # ----------------------------------------------------------
                raw_bytes: bytes = resp.content
                if raw_bytes[:2] == b"\x1f\x8b":
                    try:
                        raw_bytes = gzip.decompress(raw_bytes)
                        self.logger.debug(
                            "[universparadiscount] Gzip-decompressed sitemap page %d.",
                            page,
                        )
                    except Exception as exc:
                        self.logger.error(
                            "[universparadiscount] Gzip decompression failed page %d: %s",
                            page, exc,
                        )
                        page += 1
                        await self._delay()
                        continue

                # ----------------------------------------------------------
                # Sanitise — strip XML-illegal control characters that cause
                # ET.ParseError on malformed PrestaShop sitemaps.
                # Keeps: tab (0x09), newline (0x0A), carriage return (0x0D),
                # and all printable characters (0x20+).
                # ----------------------------------------------------------
                xml_text = raw_bytes.decode("utf-8", errors="replace")
                xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", xml_text)

                # ----------------------------------------------------------
                # Parse XML
                # ----------------------------------------------------------
                try:
                    root = ET.fromstring(xml_text)
                except ET.ParseError as exc:
                    self.logger.error(
                        "[universparadiscount] XML parse error on page %d: %s",
                        page, exc,
                    )
                    page += 1
                    await self._delay()
                    continue

                all_url_elements = root.findall("sm:url", _SITEMAP_NS)
                if not all_url_elements:
                    self.logger.info(
                        "[universparadiscount] Sitemap page %d is empty — done.",
                        page,
                    )
                    break

                page_count = 0
                for url_el in all_url_elements:
                    loc_el = url_el.find("sm:loc", _SITEMAP_NS)
                    if loc_el is None or not loc_el.text:
                        continue
                    loc = loc_el.text.strip()
                    if _PRODUCT_RE.search(loc):
                        page_count += 1
                        total += 1
                        yield loc

                self.logger.info(
                    "[universparadiscount] Sitemap page %d → %d product URLs "
                    "(running total: %d)",
                    page, page_count, total,
                )

                page += 1
                await self._delay()

        self.logger.info(
            "[universparadiscount] Discovery complete — %d product URLs total.",
            total,
        )
