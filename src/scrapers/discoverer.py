"""
src/scrapers/discoverer.py
===========================
URL Discovery Engine — Bronze layer, Phase 0.

Responsibilities
----------------
1. Load all active ``SiteConfig`` records from the Django DB.
2. Select the correct discovery plugin for each site (from ``PLUGIN_REGISTRY``).
3. Run the plugin's ``discover()`` generator to collect product URLs.
4. Bulk-upsert discovered URLs into ``ScrapedURL`` (idempotent — safe to re-run).
5. Enqueue ``fetch_product_page`` Arq jobs for every PENDING URL.

Architecture contract (CLAUDE.md §1)
--------------------------------------
- The discoverer NEVER fetches product pages or parses HTML content.
- It NEVER writes raw data to R2 — that is the Arq worker's job.
- All DB writes use Django's async ORM (``abulk_create``, ``aupdate_or_create``).
- Proxy selection draws from the live ``ProxyPool`` queryset, not env vars,
  so disabling a proxy in Admin takes effect immediately.

Running standalone
------------------
    DJANGO_SETTINGS_MODULE=core.settings python -m src.scrapers.discoverer

Running via Dagster
-------------------
    The ``bronze_discovery`` Dagster asset imports and calls ``run_discovery()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

# Load src/scrapers/.env — must happen before Django bootstrap and os.getenv() calls
load_dotenv(Path(__file__).resolve().parent / ".env")

# ---------------------------------------------------------------------------
# Django bootstrap (must happen before any ORM import)
# ---------------------------------------------------------------------------
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django  # noqa: E402
django.setup()

from arq import create_pool  # noqa: E402
from arq.connections import RedisSettings  # noqa: E402
from asgiref.sync import sync_to_async  # noqa: E402
from django.db import models, transaction  # noqa: E402

from scraper_admin.models import ProxyPool, ScrapedURL, SiteConfig  # noqa: E402
from scrapers.plugins import (  # noqa: E402
    BaseDiscoveryPlugin,
    CategoryCrawlPlugin,
    DiscoveryContext,
    HybridSitemapCategoryPlugin,
    JsonLdApiPlugin,
    SitemapXMLPlugin,
)
from scrapers.plugins.sites.universparadiscount import UniversparadiscountPlugin  # noqa: E402
from scrapers.plugins.sites.shopify import ShopifyPlugin  # noqa: E402
from scrapers.plugins.sites.woocommerce import WooCommercePlugin  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)


# ===========================================================================
# PLUGIN REGISTRY
# ===========================================================================
# Maps domain → (PluginClass, extra_config dict).
#
# HOW TO ADD A NEW SITE:
# 1. Choose the correct strategy from plugins/strategies.py (or write a new one).
# 2. Add an entry below keyed by the site's exact domain string.
# 3. Fill ``extra_config`` with selector/API details for that site.
# 4. Add the SiteConfig row in Django Admin.
# 5. Run the discoverer — no code changes needed elsewhere.
#
# The ``extra_config`` dict is passed as the second argument to the plugin
# constructor and is fully site-specific.  See each plugin class for the
# supported keys.
# ===========================================================================

PLUGIN_REGISTRY: dict[str, tuple[type[BaseDiscoveryPlugin], dict]] = {

    # -----------------------------------------------------------------------
    # universparadiscount.ma — PrestaShop, paginated gzip XML sitemap
    # -----------------------------------------------------------------------
    "universparadiscount.ma": (UniversparadiscountPlugin, {}),

    # -----------------------------------------------------------------------
    # Shopify stores — generic plugin, works for any Shopify domain
    # -----------------------------------------------------------------------
    "beautymarket.ma": (ShopifyPlugin, {}),

    # -----------------------------------------------------------------------
    # WooCommerce + Yoast stores — generic plugin, works for any WooCommerce domain
    # -----------------------------------------------------------------------
    "cotepara.ma": (WooCommercePlugin, {}),
    "beautymall.ma": (WooCommercePlugin, {}),
    "parachezvous.ma": (WooCommercePlugin, {}),

    # -----------------------------------------------------------------------
    # Sites using pure XML sitemap discovery
    # -----------------------------------------------------------------------
    "www.pharma-gdd.com": (
        SitemapXMLPlugin,
        {},  # sitemap_url comes from SiteConfig
    ),
    "www.parapharmacie.fr": (
        SitemapXMLPlugin,
        {},
    ),
    "www.mon-pharmacien-conseil.com": (
        SitemapXMLPlugin,
        {},
    ),

    # -----------------------------------------------------------------------
    # Sites using category-page crawl with CSS selectors
    # -----------------------------------------------------------------------
    "www.beaute-sante.com": (
        CategoryCrawlPlugin,
        {
            "category_urls": [
                "https://www.beaute-sante.com/soins-du-visage/",
                "https://www.beaute-sante.com/complements-alimentaires/",
                "https://www.beaute-sante.com/solaire/",
                "https://www.beaute-sante.com/bebe-et-maternite/",
            ],
            "product_link_selector": "a.product-card__link",
            "next_page_selector": "a[rel='next']",
            "max_pages": 150,
            "url_prefix": "https://www.beaute-sante.com",
        },
    ),
    "www.pharmacie-lafayette.com": (
        CategoryCrawlPlugin,
        {
            "category_urls": [
                "https://www.pharmacie-lafayette.com/beaute/",
                "https://www.pharmacie-lafayette.com/sante/",
                "https://www.pharmacie-lafayette.com/bebe/",
            ],
            "product_link_selector": "h2.product-title > a",
            "next_page_selector": "li.next > a",
            "max_pages": 200,
            "url_prefix": "https://www.pharmacie-lafayette.com",
        },
    ),

    # -----------------------------------------------------------------------
    # Sites with JSON-LD embedded on category pages
    # -----------------------------------------------------------------------
    "www.mypharmacine.com": (
        JsonLdApiPlugin,
        {
            "api_mode": False,    # JSON-LD extraction, not REST API
            "category_urls": [
                "https://www.mypharmacine.com/dermato-cosmetologie/",
                "https://www.mypharmacine.com/complements-alimentaires/",
                "https://www.mypharmacine.com/bebe-et-enfant/",
                "https://www.mypharmacine.com/solaire/",
            ],
        },
    ),

    # -----------------------------------------------------------------------
    # Sites exposing a REST/JSON API for product listings
    # -----------------------------------------------------------------------
    "www.easypara.fr": (
        JsonLdApiPlugin,
        {
            "api_mode": True,
            "api_endpoint": "https://www.easypara.fr/api/products",
            "page_param": "page",
            "page_size_param": "limit",
            "page_size": 100,
            "url_field": "url",           # item["url"]
            "max_pages": 500,
        },
    ),
    "www.parapharmaciedirect.com": (
        JsonLdApiPlugin,
        {
            "api_mode": True,
            "api_endpoint": "https://www.parapharmaciedirect.com/api/v2/catalog",
            "page_param": "p",
            "page_size_param": "n",
            "page_size": 48,
            "url_field": "links.canonical",   # item["links"]["canonical"]
            "max_pages": 300,
        },
    ),

    # -----------------------------------------------------------------------
    # Sites using the hybrid strategy (sitemap + category gap-fill)
    # -----------------------------------------------------------------------
    "www.doctipharma.fr": (
        HybridSitemapCategoryPlugin,
        {
            # SitemapXMLPlugin will use ctx.sitemap_url from SiteConfig
            # CategoryCrawlPlugin seeds:
            "category_urls": [
                "https://www.doctipharma.fr/beaute-soins",
                "https://www.doctipharma.fr/complements",
                "https://www.doctipharma.fr/bebe",
            ],
            "product_link_selector": "a.product-name",
            "next_page_selector": "a.pagination-next",
            "max_pages": 100,
            "url_prefix": "https://www.doctipharma.fr",
        },
    ),
}


# ===========================================================================
# R2 / Proxy helpers
# ===========================================================================

async def _pick_random_proxy(domain: str | None = None) -> str | None:
    """
    Return a random active proxy DSN from the Django ProxyPool.

    Selection priority:
    1. Site-specific proxies (linked to the given domain via M2M)
    2. Global proxies (no site restriction)
    3. None (host IP) if no active proxies found

    Parameters
    ----------
    domain:
        The site domain being scraped. Used to prefer site-specific proxies.
    """
    # Try site-specific proxies first
    if domain:
        site_proxies = await sync_to_async(
            lambda: list(
                ProxyPool.objects.filter(
                    is_active=True,
                    sites__domain=domain,
                ).values_list("endpoint", flat=True)
            )
        )()
        if site_proxies:
            logger.debug("Using site-specific proxy for %s.", domain)
            return random.choice(site_proxies)

    # Fall back to global proxies (no site restriction)
    global_proxies = await sync_to_async(
        lambda: list(
            ProxyPool.objects.filter(
                is_active=True,
                sites__isnull=True,
            ).values_list("endpoint", flat=True)
        )
    )()
    if global_proxies:
        return random.choice(global_proxies)

    logger.warning("No active proxies in ProxyPool. Discovery will use host IP.")
    return None


# ===========================================================================
# URL upsert helpers
# ===========================================================================

# How many days before a done URL is eligible for re-scraping
RESCRAPE_AFTER_DAYS: int = int(os.getenv("DISCOVERER_RESCRAPE_AFTER_DAYS", "30"))

UPSERT_BATCH_SIZE: int = int(os.getenv("DISCOVERER_UPSERT_BATCH_SIZE", "500"))


async def _bulk_upsert_urls(
    site: SiteConfig,
    urls: list[str],
    *,
    rescrape: bool = False,
) -> tuple[int, int]:
    """
    Idempotently insert discovered URLs into the ScrapedURL table.

    For new URLs: insert with status=pending.
    For existing pending/in_progress URLs: leave untouched.
    For existing done/failed/blocked URLs:
      - If rescrape=True: always reset to pending.
      - If rescrape=False: only reset if next_scrape_after has passed.

    Parameters
    ----------
    site:
        The SiteConfig instance these URLs belong to.
    urls:
        List of fully-qualified product URL strings.
    rescrape:
        If True, force-reset all done/failed/blocked URLs back to pending
        regardless of next_scrape_after.

    Returns
    -------
    tuple[int, int]
        (rows_created_or_reset, rows_skipped)
    """
    if not urls:
        return 0, 0

    now = datetime.now(timezone.utc)

    objs = [
        ScrapedURL(
            site=site,
            url=url,
            url_type=ScrapedURL.URLType.PRODUCT,
            status=ScrapedURL.Status.PENDING,
            next_scrape_after=None,
        )
        for url in urls
    ]

    @sync_to_async
    def _do_upsert(batch: list[ScrapedURL]) -> tuple[int, int]:
        with transaction.atomic():
            # Insert new URLs — skip existing ones
            created = ScrapedURL.objects.bulk_create(
                batch,
                ignore_conflicts=True,
            )

            batch_urls = [obj.url for obj in batch]
            base_qs = ScrapedURL.objects.filter(
                site=site,
                url__in=batch_urls,
                status__in=[
                    ScrapedURL.Status.DONE,
                    ScrapedURL.Status.FAILED,
                    ScrapedURL.Status.BLOCKED,
                    ScrapedURL.Status.NOT_FOUND,
                ],
            )

            if rescrape:
                # --rescrape: reset everything regardless of next_scrape_after
                reset_count = base_qs.update(
                    status=ScrapedURL.Status.PENDING,
                    arq_job_id="",
                    next_scrape_after=None,
                )
            else:
                # Normal run: only reset URLs whose next_scrape_after has passed
                reset_count = base_qs.filter(
                    models.Q(next_scrape_after__isnull=True)
                    | models.Q(next_scrape_after__lte=now)
                ).update(
                    status=ScrapedURL.Status.PENDING,
                    arq_job_id="",
                    next_scrape_after=None,
                )

        return len(created), reset_count

    created_total = 0
    reset_total = 0
    for i in range(0, len(objs), UPSERT_BATCH_SIZE):
        batch = objs[i : i + UPSERT_BATCH_SIZE]
        created, reset = await _do_upsert(batch)
        created_total += created
        reset_total += reset
        logger.debug(
            "Upserted batch of %d for %s — %d new, %d reset.",
            len(batch), site.domain, created, reset,
        )

    if reset_total:
        reason = "forced rescrape" if rescrape else "next_scrape_after passed"
        logger.info(
            "[%s] %d URLs reset to pending (%s).",
            site.domain, reset_total, reason,
        )
    return created_total + reset_total, len(urls) - created_total - reset_total


# ===========================================================================
# Arq job enqueue
# ===========================================================================

ENQUEUE_BATCH_SIZE: int = int(os.getenv("DISCOVERER_ENQUEUE_BATCH_SIZE", "100"))


async def _enqueue_pending_urls(
    redis_pool,
    site: SiteConfig,
) -> int:
    """
    Enqueue PENDING and orphaned IN_PROGRESS URLs as Arq jobs.

    Arq deduplicates by job ID — if a job already exists in Redis,
    enqueue_job returns None and we skip it. Safe to call while the
    worker is already running.
    """
    enqueued = 0

    @sync_to_async
    def _fetch_pending_batch(offset: int) -> list[ScrapedURL]:
        return list(
            ScrapedURL.objects.filter(
                site=site,
                status__in=[
                    ScrapedURL.Status.PENDING,
                    ScrapedURL.Status.IN_PROGRESS,
                ],
                url_type=ScrapedURL.URLType.PRODUCT,
            ).order_by("-priority", "discovered_at")[offset : offset + ENQUEUE_BATCH_SIZE]
        )

    @sync_to_async
    def _mark_in_progress(url_id: UUID, job_id: str) -> None:
        ScrapedURL.objects.filter(id=url_id).update(
            status=ScrapedURL.Status.IN_PROGRESS,
            arq_job_id=job_id,
        )

    offset = 0
    while True:
        batch = await _fetch_pending_batch(offset)
        if not batch:
            break

        for scraped_url in batch:
            job = await redis_pool.enqueue_job(
                "fetch_product_page",
                scraped_url.url,
                scraped_url_id=str(scraped_url.id),
                site_id=str(scraped_url.site_id),
            )
            if job:
                await _mark_in_progress(scraped_url.id, job.job_id)
                enqueued += 1

        offset += ENQUEUE_BATCH_SIZE
        logger.info(
            "Enqueued %d jobs for %s (offset %d).", len(batch), site.domain, offset
        )

    return enqueued


# ===========================================================================
# Per-site discovery runner
# ===========================================================================

async def _discover_site(
    site: SiteConfig,
    redis_pool,
    *,
    enqueue: bool = True,
    rescrape: bool = False,
) -> dict:
    """
    Run the full discovery pipeline for a single site.

    Parameters
    ----------
    site:
        Active SiteConfig instance.
    redis_pool:
        Arq Redis pool for job enqueuing.
    enqueue:
        If False, only discover + upsert; skip enqueueing.
    rescrape:
        If True, reset all done/failed/blocked URLs back to pending
        so they get re-scraped regardless of next_scrape_after.
    """
    plugin_entry = PLUGIN_REGISTRY.get(site.domain)
    if not plugin_entry:
        logger.warning(
            "No plugin registered for domain '%s' — skipping.", site.domain
        )
        return {
            "site": site.domain,
            "error": "no_plugin_registered",
            "urls_found": 0,
        }

    plugin_class, extra_config = plugin_entry
    proxy = await _pick_random_proxy(domain=site.domain)

    ctx = DiscoveryContext(
        site_id=site.id,
        site_name=site.name,
        domain=site.domain,
        base_url=site.base_url,
        request_delay_ms=site.request_delay_ms,
        max_concurrency=site.max_concurrency,
        max_retries=site.max_retries,
        sitemap_url=site.sitemap_url,
        category_url_patterns=site.category_url_patterns or [],
        product_url_patterns=site.product_url_patterns or [],
        proxy=proxy,
        impersonate_profile=site.impersonate_profile,
    )

    # Instantiate plugin — some take only ctx, others take ctx + extra_config
    try:
        if extra_config:
            plugin: BaseDiscoveryPlugin = plugin_class(ctx, extra_config)
        else:
            plugin = plugin_class(ctx)
    except TypeError:
        # Fallback: try without extra_config (SitemapXMLPlugin only takes ctx)
        plugin = plugin_class(ctx)

    logger.info(
        "Starting discovery for %s using %s.",
        site.domain, plugin_class.__name__,
    )

    # Collect URLs from the plugin in batches and upsert as we go
    url_buffer: list[str] = []
    total_found = 0
    total_created = 0
    total_skipped = 0

    async for url in plugin.discover():
        url_buffer.append(url)
        total_found += 1

        if len(url_buffer) >= UPSERT_BATCH_SIZE:
            created, skipped = await _bulk_upsert_urls(site, url_buffer, rescrape=rescrape)
            total_created += created
            total_skipped += skipped
            url_buffer.clear()
            logger.info(
                "[%s] Progress: %d URLs discovered, %d new.",
                site.domain, total_found, total_created,
            )

    # Flush remainder
    if url_buffer:
        created, skipped = await _bulk_upsert_urls(site, url_buffer, rescrape=rescrape)
        total_created += created
        total_skipped += skipped

    logger.info(
        "[%s] Discovery complete: %d URLs found | %d new | %d existing.",
        site.domain, total_found, total_created, total_skipped,
    )

    # Enqueue Arq jobs
    jobs_enqueued = 0
    if enqueue:
        jobs_enqueued = await _enqueue_pending_urls(redis_pool, site)
        logger.info("[%s] %d Arq jobs enqueued.", site.domain, jobs_enqueued)

    return {
        "site": site.domain,
        "plugin": plugin_class.__name__,
        "urls_found": total_found,
        "urls_created": total_created,
        "urls_skipped": total_skipped,
        "jobs_enqueued": jobs_enqueued,
        "run_at": datetime.now(timezone.utc).isoformat(),
    }


# ===========================================================================
# Public entry point
# ===========================================================================

async def run_discovery(
    *,
    site_domains: list[str] | None = None,
    enqueue: bool = True,
    rescrape: bool = False,
    max_concurrent_sites: int = 3,
) -> list[dict]:
    """
    Run URL discovery for all active sites (or a filtered subset).

    Parameters
    ----------
    site_domains:
        If provided, only discover these domains.
    enqueue:
        If True (default), enqueue Arq jobs after upserting URLs.
    rescrape:
        If True, reset all done/failed/blocked URLs back to pending
        so they get re-scraped. Use when you want to re-scrape everything.
    max_concurrent_sites:
        Number of sites to discover concurrently.
    """
    # Load active sites from Django ORM
    @sync_to_async
    def _load_sites() -> list[SiteConfig]:
        qs = SiteConfig.objects.filter(status=SiteConfig.Status.ACTIVE)
        if site_domains:
            qs = qs.filter(domain__in=site_domains)
        return list(qs.order_by("name"))

    sites = await _load_sites()
    if not sites:
        logger.warning("No active SiteConfig records found.  Nothing to discover.")
        return []

    logger.info(
        "Discovery starting: %d active site(s). enqueue=%s concurrency=%d.",
        len(sites), enqueue, max_concurrent_sites,
    )

    # Create Arq Redis pool
    redis_pool = await create_pool(
        RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    )

    try:
        # Semaphore to cap simultaneous site discoveries
        sem = asyncio.Semaphore(max_concurrent_sites)

        async def _bounded_discover(site: SiteConfig) -> dict:
            async with sem:
                return await _discover_site(
                    site, redis_pool, enqueue=enqueue, rescrape=rescrape
                )

        tasks = [_bounded_discover(site) for site in sites]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await redis_pool.aclose()

    summaries: list[dict] = []
    for site, result in zip(sites, results):
        if isinstance(result, Exception):
            logger.error("Discovery failed for %s: %s", site.domain, result)
            summaries.append({"site": site.domain, "error": str(result)})
        else:
            summaries.append(result)

    # Print summary table
    logger.info("=" * 60)
    logger.info("Discovery run complete.")
    for s in summaries:
        if "error" in s:
            logger.error("  ✗ %s — %s", s["site"], s["error"])
        else:
            logger.info(
                "  ✓ %-35s  found=%d  new=%d  jobs=%d",
                s["site"], s.get("urls_found", 0),
                s.get("urls_created", 0), s.get("jobs_enqueued", 0),
            )
    logger.info("=" * 60)

    return summaries


# ===========================================================================
# CLI entry point
# ===========================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run URL discovery for pipeline sites.")
    parser.add_argument(
        "--sites",
        nargs="*",
        help="Limit discovery to these domain names (default: all active sites).",
    )
    parser.add_argument(
        "--no-enqueue",
        action="store_true",
        help="Discover and upsert URLs without enqueueing Arq jobs.",
    )
    parser.add_argument(
        "--rescrape",
        action="store_true",
        help=(
            "Reset all done/failed/blocked URLs back to pending so they "
            "get re-scraped. Use when you want to re-scrape everything from scratch."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Max concurrent site discoveries (default: 3).",
    )
    args = parser.parse_args()

    asyncio.run(
        run_discovery(
            site_domains=args.sites or None,
            enqueue=not args.no_enqueue,
            rescrape=args.rescrape,
            max_concurrent_sites=args.concurrency,
        )
    )
