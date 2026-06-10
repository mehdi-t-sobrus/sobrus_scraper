"""src/scrapers/plugins/__init__.py"""
from .base import BaseDiscoveryPlugin, DiscoveryContext
from .strategies import (
    SitemapXMLPlugin,
    CategoryCrawlPlugin,
    JsonLdApiPlugin,
    HybridSitemapCategoryPlugin,
)

__all__ = [
    "BaseDiscoveryPlugin",
    "DiscoveryContext",
    "SitemapXMLPlugin",
    "CategoryCrawlPlugin",
    "JsonLdApiPlugin",
    "HybridSitemapCategoryPlugin",
]
