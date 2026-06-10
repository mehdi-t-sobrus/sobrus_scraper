"""
src/scrapers/plugins/base.py
============================
Abstract base class and shared types for all site-specific discovery plugins.

Every plugin implements exactly one method: ``discover()``.
The discoverer calls it and receives an async generator of raw URL strings.
The plugin owns how it finds those URLs — sitemap, category crawl, API, etc.

Plugin contract
---------------
- NEVER write to the database.  Return URLs; the discoverer bulk-upserts them.
- NEVER parse product data.  That belongs to the Silver layer.
- Use ``curl_cffi.requests.AsyncSession`` for all HTTP.  Never ``requests``.
- Respect the ``SiteConfig`` throttle fields passed in via ``DiscoveryContext``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator
from uuid import UUID

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared context object injected into every plugin
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryContext:
    """
    Runtime context passed from the discoverer to every plugin.

    All fields mirror the ``SiteConfig`` Django model so plugins can
    make live-adjusted throttling decisions without a DB round-trip.
    """

    site_id: UUID
    site_name: str
    domain: str
    base_url: str

    # -- Throttle (from SiteConfig live values) ------------------------------
    request_delay_ms: int = 1_000
    max_concurrency: int = 5
    max_retries: int = 3

    # -- Discovery config (from SiteConfig JSON fields) ----------------------
    sitemap_url: str = ""
    category_url_patterns: list[str] = field(default_factory=list)
    product_url_patterns: list[str] = field(default_factory=list)

    # -- Optional proxy DSN (picked by discoverer from ProxyPool) ------------
    proxy: str | None = None

    # -- Impersonation profile override --------------------------------------
    impersonate_profile: str = "chrome"


# ---------------------------------------------------------------------------
# Abstract base plugin
# ---------------------------------------------------------------------------

class BaseDiscoveryPlugin(ABC):
    """
    Abstract base class for all site-specific URL discovery plugins.

    Subclass this and implement ``discover()`` to support a new site.
    Register the subclass in ``PLUGIN_REGISTRY`` inside ``discoverer.py``.

    Example
    -------
    .. code-block:: python

        class MypharmaciePlugin(BaseDiscoveryPlugin):
            async def discover(self) -> AsyncIterator[str]:
                async for url in self._iter_sitemap(self.ctx.sitemap_url):
                    yield url
    """

    def __init__(self, ctx: DiscoveryContext) -> None:
        self.ctx = ctx
        self.logger = logging.getLogger(
            f"{__name__}.{type(self).__name__}"
        )

    @abstractmethod
    async def discover(self) -> AsyncIterator[str]:
        """
        Async generator that yields fully-qualified product page URLs.

        Must be implemented by every plugin.  The discoverer collects
        yielded URLs and bulk-upserts them into the Django DB.

        Yields
        ------
        str
            A fully-qualified product URL, e.g.
            ``https://www.example.com/products/crème-solaire-spf50``
        """
        ...  # pragma: no cover

    # -----------------------------------------------------------------------
    # Shared helpers available to all plugins
    # -----------------------------------------------------------------------

    async def _delay(self) -> None:
        """Honour the per-site request delay from SiteConfig."""
        import asyncio
        delay_s = self.ctx.request_delay_ms / 1_000
        await asyncio.sleep(delay_s)

    def _proxies(self) -> dict[str, str] | None:
        """Return a curl_cffi-compatible proxies dict, or None."""
        if not self.ctx.proxy:
            return None
        return {"http": self.ctx.proxy, "https": self.ctx.proxy}

    def _matches_product_pattern(self, url: str) -> bool:
        """
        Return True if ``url`` matches at least one product URL pattern
        configured in ``SiteConfig.product_url_patterns``.

        If no patterns are configured, every URL is considered a product URL.
        """
        import re
        if not self.ctx.product_url_patterns:
            return True
        return any(
            re.search(pat, url)
            for pat in self.ctx.product_url_patterns
        )
