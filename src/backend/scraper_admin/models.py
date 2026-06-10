"""
src/backend/scraper_admin/models.py
====================================
Django models for the scraper control plane.

These models are the live command surface for the pipeline:
  - SiteConfig  → defines a target site and its throttling rules
  - ProxyPool   → rotating proxy endpoints drawn by Arq workers
  - ScrapedURL  → the URL inventory / job queue (written by discoverer.py)
  - ScrapeLog   → per-job immutable audit trail written by Arq workers

Design rules (CLAUDE.md §1):
  * Arq workers READ SiteConfig/ProxyPool and WRITE ScrapeLog rows only.
  * Workers never write to ScrapedURL directly — that's the discoverer's domain.
  * TimescaleDB hypertable promotion for ScrapeLog is handled in a custom
    migration (see migrations/0002_scrapelog_hypertable.py).
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


# ---------------------------------------------------------------------------
# SiteConfig — Target site registry + per-domain throttle settings
# ---------------------------------------------------------------------------

class SiteConfig(models.Model):
    """
    Represents a single target parapharmacy / e-commerce site.

    The Django Admin exposes this model as the live throttle dashboard.
    Workers read ``max_concurrency`` and ``request_delay_ms`` on every job
    so changes take effect immediately without a redeploy.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        PAUSED = "paused", _("Paused")
        BLOCKED = "blocked", _("Blocked — under investigation")
        ARCHIVED = "archived", _("Archived")

    # -- Identity ------------------------------------------------------------
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=128,
        unique=True,
        help_text=_("Human-readable site name, e.g. 'Pharma Plus FR'"),
    )
    base_url = models.URLField(
        max_length=512,
        unique=True,
        help_text=_("Root URL of the target site, e.g. https://example.com"),
    )
    domain = models.CharField(
        max_length=253,
        unique=True,
        db_index=True,
        help_text=_("Bare hostname used for per-domain semaphore keying, e.g. example.com"),
    )

    # -- Status --------------------------------------------------------------
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    # -- Throttle settings (CLAUDE.md §4) ------------------------------------
    max_concurrency = models.PositiveSmallIntegerField(
        default=5,
        help_text=_(
            "Max simultaneous Arq workers hitting this domain. "
            "Hard cap per CLAUDE.md §4 is 5 — do not exceed without review."
        ),
    )
    request_delay_ms = models.PositiveIntegerField(
        default=1_000,
        help_text=_("Minimum delay between sequential requests to this domain (ms)."),
    )
    retry_backoff_base_seconds = models.FloatField(
        default=5.0,
        help_text=_("Base seconds for exponential back-off on retries."),
    )
    max_retries = models.PositiveSmallIntegerField(
        default=3,
        help_text=_("Maximum fetch retry attempts per URL."),
    )

    # -- Sitemap / discovery -------------------------------------------------
    sitemap_url = models.URLField(
        max_length=512,
        blank=True,
        default="",
        help_text=_("Primary sitemap URL for the URL discoverer, if available."),
    )
    category_url_patterns = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of regex patterns matching category/listing page URLs "
            "that the discoverer should follow."
        ),
    )
    product_url_patterns = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "List of regex patterns identifying product detail page URLs."
        ),
    )

    # -- Impersonation override ----------------------------------------------
    impersonate_profile = models.CharField(
        max_length=32,
        default="chrome",
        help_text=_(
            "curl_cffi browser impersonation profile override for this site. "
            "Defaults to the global SCRAPER_IMPERSONATE_PROFILE env var."
        ),
    )

    # -- Metadata ------------------------------------------------------------
    notes = models.TextField(
        blank=True,
        default="",
        help_text=_("Internal ops notes — anti-bot measures, quirks, contact info."),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site Configuration"
        verbose_name_plural = "Site Configurations"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.domain}) [{self.status}]"


# ---------------------------------------------------------------------------
# ProxyPool — Rotating proxy endpoint registry
# ---------------------------------------------------------------------------

class ProxyPool(models.Model):
    """
    A single proxy endpoint available to Arq scraping workers.

    Workers call the API (or read a cached queryset) to pull an active proxy.
    Marking a proxy ``is_active=False`` via the Admin immediately removes it
    from the rotation on the next job cycle.
    """

    class ProxyType(models.TextChoices):
        RESIDENTIAL = "residential", _("Residential")
        DATACENTER = "datacenter", _("Datacenter")
        MOBILE = "mobile", _("Mobile")
        ISP = "isp", _("ISP")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Full DSN: http://user:pass@host:port  (credentials stored in DB, not env)
    endpoint = models.CharField(
        max_length=512,
        unique=True,
        help_text=_("Full proxy DSN: http://user:pass@host:port"),
    )
    proxy_type = models.CharField(
        max_length=16,
        choices=ProxyType.choices,
        default=ProxyType.RESIDENTIAL,
    )
    provider = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_("Provider name, e.g. 'BrightData', 'Oxylabs'."),
    )
    country_code = models.CharField(
        max_length=2,
        blank=True,
        default="",
        help_text=_("ISO 3166-1 alpha-2 country code of the exit node."),
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text=_("Uncheck to immediately remove from worker rotation."),
    )

    # -- Health tracking (updated by a periodic Arq health-check job) --------
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Proxy"
        verbose_name_plural = "Proxy Pool"
        ordering = ["provider", "country_code"]

    def __str__(self) -> str:
        # Never expose credentials in string representation
        try:
            from urllib.parse import urlparse
            parsed = urlparse(self.endpoint)
            safe = f"{parsed.scheme}://***@{parsed.hostname}:{parsed.port}"
        except Exception:
            safe = "***"
        return f"{self.provider} [{self.proxy_type}] — {safe}"


# ---------------------------------------------------------------------------
# ScrapedURL — URL Inventory / Job Queue
# ---------------------------------------------------------------------------

class ScrapedURL(models.Model):
    """
    A single product or category URL discovered by ``discoverer.py``.

    This model is the ingestion job queue.  Dagster reads pending rows,
    enqueues them as Arq jobs, and marks them as ``in_progress``.
    On completion, the Arq worker writes a ``ScrapeLog`` row and updates
    the status here via the async ORM.

    Bulk inserts/updates use ``bulk_create(update_conflicts=True)`` to keep
    the discoverer pass idempotent (CLAUDE.md §1).
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending — awaiting scrape")
        IN_PROGRESS = "in_progress", _("In Progress — job enqueued")
        DONE = "done", _("Done — successfully scraped")
        BLOCKED = "blocked", _("Blocked — 403 received")
        NOT_FOUND = "not_found", _("Not Found — 404/410")
        FAILED = "failed", _("Failed — exhausted retries")
        EXCLUDED = "excluded", _("Excluded — manually suppressed")

    class URLType(models.TextChoices):
        PRODUCT = "product", _("Product Detail Page")
        CATEGORY = "category", _("Category / Listing Page")
        SITEMAP = "sitemap", _("Sitemap")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    site = models.ForeignKey(
        SiteConfig,
        on_delete=models.CASCADE,
        related_name="scraped_urls",
        db_index=True,
    )

    # -- URL identity --------------------------------------------------------
    url = models.URLField(
        max_length=2048,
        db_index=True,
        help_text=_("Fully-qualified product or category page URL."),
    )
    url_type = models.CharField(
        max_length=16,
        choices=URLType.choices,
        default=URLType.PRODUCT,
        db_index=True,
    )

    # -- Queue state ---------------------------------------------------------
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    priority = models.SmallIntegerField(
        default=0,
        db_index=True,
        help_text=_("Higher = scraped sooner.  Use negative values to deprioritise."),
    )

    # -- Arq job tracking ----------------------------------------------------
    arq_job_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=_("Arq job ID currently processing this URL, if any."),
    )

    # -- Timestamps ----------------------------------------------------------
    discovered_at = models.DateTimeField(auto_now_add=True)
    last_scraped_at = models.DateTimeField(null=True, blank=True)
    next_scrape_after = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Earliest time this URL should be re-scraped.  NULL = immediately."),
    )

    class Meta:
        verbose_name = "Scraped URL"
        verbose_name_plural = "Scraped URLs"
        ordering = ["-priority", "discovered_at"]
        constraints = [
            # Enforce URL uniqueness per site (same URL can appear on multiple sites)
            models.UniqueConstraint(fields=["site", "url"], name="unique_site_url"),
        ]
        indexes = [
            # Optimise the Dagster sensor query: pending product URLs, highest priority first
            models.Index(
                fields=["status", "url_type", "-priority", "next_scrape_after"],
                name="idx_scraped_url_queue",
            ),
        ]

    def __str__(self) -> str:
        return f"[{self.status}] {self.url[:80]}"


# ---------------------------------------------------------------------------
# ScrapeLog — Immutable per-job audit trail
# ---------------------------------------------------------------------------

class ScrapeLog(models.Model):
    """
    Immutable record of a single Arq fetch attempt.

    Written once by the Arq worker on job completion (success or failure).
    Never updated after creation — treat rows as append-only.

    This table is declared as a TimescaleDB hypertable in migration
    0002_scrapelog_hypertable.py, partitioned on ``fetched_at``.
    Do NOT run bulk DELETEs or UPDATEs on this table (CLAUDE.md §3).
    """

    class FetchStatus(models.TextChoices):
        SUCCESS = "success", _("Success")
        BLOCKED = "blocked", _("Blocked (403)")
        NOT_FOUND = "not_found", _("Not Found (404/410)")
        TIMEOUT = "timeout", _("Timeout")
        RATE_LIMITED = "rate_limited", _("Rate Limited (429)")
        HTTP_ERROR = "http_error", _("HTTP Error")
        NETWORK_ERROR = "network_error", _("Network Error")
        UNKNOWN_ERROR = "unknown_error", _("Unknown Error")

    # TimescaleDB requires the partition column (fetched_at) to be part of
    # any unique index on the table. A UUID-only primary key creates a unique
    # index that doesn't include fetched_at, which TimescaleDB rejects.
    # Solution: use BigAutoField as PK (sequential, no unique index conflict)
    # and keep uuid as a non-unique indexed field for external references.
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    scraped_url = models.ForeignKey(
        ScrapedURL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="scrape_logs",
    )
    site = models.ForeignKey(
        SiteConfig,
        on_delete=models.SET_NULL,
        null=True,
        related_name="scrape_logs",
        db_index=True,
    )

    # -- Request identity ----------------------------------------------------
    url = models.URLField(
        max_length=2048,
        help_text=_("Exact URL fetched (may differ from ScrapedURL after redirects)."),
    )
    final_url = models.URLField(
        max_length=2048,
        blank=True,
        default="",
        help_text=_("Final URL after all redirects."),
    )

    # -- Outcome -------------------------------------------------------------
    status = models.CharField(
        max_length=16,
        choices=FetchStatus.choices,
        db_index=True,
    )
    http_status_code = models.SmallIntegerField(null=True, blank=True)

    # -- Telemetry -----------------------------------------------------------
    elapsed_seconds = models.FloatField(
        help_text=_("Total request duration including retries."),
    )
    attempt_count = models.PositiveSmallIntegerField(default=1)
    content_length_bytes = models.IntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=128, blank=True, default="")

    # -- Infrastructure ------------------------------------------------------
    proxy_used = models.CharField(
        max_length=256,
        blank=True,
        default="",
        help_text=_("Obfuscated proxy DSN used for this request."),
    )
    arq_job_id = models.CharField(max_length=128, blank=True, default="")
    worker_hostname = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=_("Docker container / hostname that ran this job."),
    )

    # -- R2 pointer ----------------------------------------------------------
    r2_bronze_key = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=_(
            "Cloudflare R2 object key for the raw .jsonl.gz Bronze file "
            "produced by this fetch."
        ),
    )

    # -- Error detail --------------------------------------------------------
    error_message = models.TextField(blank=True, default="")

    # -- Time axis (TimescaleDB partitions on this column) -------------------
    fetched_at = models.DateTimeField(
        db_index=True,
        help_text=_("UTC timestamp when the fetch completed."),
    )

    class Meta:
        verbose_name = "Scrape Log"
        verbose_name_plural = "Scrape Logs"
        ordering = ["-fetched_at"]
        indexes = [
            models.Index(fields=["site", "-fetched_at"], name="idx_scrapelog_site_time"),
            models.Index(fields=["status", "-fetched_at"], name="idx_scrapelog_status_time"),
        ]
        # Append-only contract — enforced at the application layer, not DB.
        # TimescaleDB hypertable declared in migration 0002.

    def __str__(self) -> str:
        return f"[{self.status}] {self.url[:60]} @ {self.fetched_at:%Y-%m-%d %H:%M:%S}"
