"""
src/backend/products/models.py
===============================
Django models for the Gold data warehouse layer.

These are the final, unified, deduplicated entities produced by the
entity resolution pipeline (src/matching/entity_res.py via RapidFuzz).

Design rules (CLAUDE.md §1 & §3):
  - MasterProduct is the canonical record — one row per real-world product.
  - SiteProduct is a per-site raw listing matched to a MasterProduct.
  - DailyPriceLog is a TimescaleDB hypertable (promoted in migration 0002).
    Never run standard Django deletes/updates on it.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


# ---------------------------------------------------------------------------
# MasterProduct — Canonical deduplicated product entity
# ---------------------------------------------------------------------------

class MasterProduct(models.Model):
    """
    A single deduplicated real-world product across all scraped sites.

    Created and maintained by ``src/matching/entity_res.py``.
    Editable via Django Admin for manual override of matched fields.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        DISCONTINUED = "discontinued", _("Discontinued")
        UNDER_REVIEW = "under_review", _("Under Review — matching needs human check")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # -- Canonical identity --------------------------------------------------
    name = models.CharField(
        max_length=512,
        db_index=True,
        help_text=_("Canonical product name as resolved by the matching pipeline."),
    )
    brand = models.CharField(max_length=128, blank=True, default="", db_index=True)
    ean = models.CharField(
        max_length=14,
        blank=True,
        default="",
        db_index=True,
        help_text=_("EAN-13 or EAN-8 barcode. Empty if unknown."),
    )
    mpn = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=_("Manufacturer Part Number."),
    )

    # -- Classification ------------------------------------------------------
    category = models.CharField(max_length=255, blank=True, default="", db_index=True)
    subcategory = models.CharField(max_length=255, blank=True, default="")
    tags = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Freeform taxonomy tags derived from site listings."),
    )

    # -- Content -------------------------------------------------------------
    description = models.TextField(blank=True, default="")
    ingredients = models.TextField(blank=True, default="")
    image_urls = models.JSONField(
        default=list,
        blank=True,
        help_text=_("List of canonical image URLs (highest-quality source wins)."),
    )

    # -- Matching metadata ---------------------------------------------------
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    match_confidence = models.FloatField(
        default=1.0,
        help_text=_(
            "RapidFuzz similarity score [0.0–1.0] from the last matching run. "
            "Scores below 0.85 are flagged for human review."
        ),
    )
    manually_verified = models.BooleanField(
        default=False,
        help_text=_("Admin has manually confirmed this entity resolution."),
    )

    # -- Timestamps ----------------------------------------------------------
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_matched_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Last time the matching pipeline updated this record."),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Master Product"
        verbose_name_plural = "Master Products"
        ordering = ["brand", "name"]
        indexes = [
            models.Index(fields=["ean"], name="idx_product_ean"),
            models.Index(fields=["brand", "name"], name="idx_product_brand_name"),
            models.Index(fields=["status", "match_confidence"], name="idx_product_review_queue"),
        ]

    def __str__(self) -> str:
        return f"{self.brand} — {self.name}" if self.brand else self.name


# ---------------------------------------------------------------------------
# SiteProduct — Per-site raw listing mapped to a MasterProduct
# ---------------------------------------------------------------------------

class SiteProduct(models.Model):
    """
    A scraped product listing from a single target site, linked to the
    canonical MasterProduct via the entity resolution pipeline.

    One MasterProduct can have many SiteProducts (one per site where it appears).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    master_product = models.ForeignKey(
        MasterProduct,
        on_delete=models.CASCADE,
        related_name="site_products",
        db_index=True,
    )
    site = models.ForeignKey(
        "scraper_admin.SiteConfig",
        on_delete=models.CASCADE,
        related_name="site_products",
        db_index=True,
    )
    scraped_url = models.OneToOneField(
        "scraper_admin.ScrapedURL",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="site_product",
    )

    # -- Raw scraped fields (pre-normalisation) ------------------------------
    raw_name = models.CharField(max_length=512)
    raw_brand = models.CharField(max_length=128, blank=True, default="")
    raw_ean = models.CharField(max_length=14, blank=True, default="")
    raw_category = models.CharField(max_length=255, blank=True, default="")
    raw_description = models.TextField(blank=True, default="")

    # -- Current pricing snapshot (updated on each scrape) -------------------
    current_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Latest scraped price in the site's currency."),
    )
    currency = models.CharField(max_length=3, default="EUR")
    in_stock = models.BooleanField(default=True, db_index=True)
    product_url = models.URLField(max_length=2048, unique=True)

    # -- Matching audit ------------------------------------------------------
    match_score = models.FloatField(
        default=0.0,
        help_text=_("RapidFuzz score against the matched MasterProduct name."),
    )

    # -- Timestamps ----------------------------------------------------------
    first_scraped_at = models.DateTimeField(auto_now_add=True)
    last_scraped_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site Product"
        verbose_name_plural = "Site Products"
        ordering = ["site", "raw_name"]
        indexes = [
            models.Index(fields=["site", "in_stock"], name="idx_site_product_stock"),
            models.Index(fields=["master_product", "site"], name="idx_site_product_master"),
        ]

    def __str__(self) -> str:
        return f"{self.site.domain} — {self.raw_name[:60]}"


# ---------------------------------------------------------------------------
# DailyPriceLog — TimescaleDB hypertable for price time-series
# ---------------------------------------------------------------------------

class DailyPriceLog(models.Model):
    """
    Immutable daily price snapshot for a SiteProduct.

    Promoted to a TimescaleDB hypertable in migration 0002, partitioned
    on ``logged_at``.  Use TimescaleDB continuous aggregates for analytics
    rather than Django ORM aggregations over full history (CLAUDE.md §3).

    Insert-only: never UPDATE or DELETE rows directly.
    """

    id = models.BigAutoField(primary_key=True)

    site_product = models.ForeignKey(
        SiteProduct,
        on_delete=models.CASCADE,
        related_name="price_logs",
        db_index=True,
    )
    master_product = models.ForeignKey(
        MasterProduct,
        on_delete=models.CASCADE,
        related_name="price_logs",
        db_index=True,
    )
    site = models.ForeignKey(
        "scraper_admin.SiteConfig",
        on_delete=models.CASCADE,
        related_name="price_logs",
        db_index=True,
    )

    # -- Price data ----------------------------------------------------------
    price = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")
    in_stock = models.BooleanField(default=True)

    # -- Provenance ----------------------------------------------------------
    # Stored as a plain integer rather than a FK because ScrapeLog's PK is
    # now composite (id, fetched_at) after TimescaleDB hypertable promotion.
    # PostgreSQL requires a standalone unique constraint on the target column
    # for FK references — which composite PKs don't provide on id alone.
    scrape_log_id = models.BigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_("ID of the ScrapeLog row that produced this price entry."),
    )

    # -- Time axis (TimescaleDB partitions on this column) -------------------
    logged_at = models.DateTimeField(
        db_index=True,
        help_text=_("UTC timestamp of the price observation."),
    )

    class Meta:
        verbose_name = "Daily Price Log"
        verbose_name_plural = "Daily Price Logs"
        ordering = ["-logged_at"]
        indexes = [
            models.Index(
                fields=["master_product", "-logged_at"],
                name="idx_pricelog_master_time",
            ),
            models.Index(
                fields=["site", "-logged_at"],
                name="idx_pricelog_site_time",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.site_product} — {self.price} {self.currency} "
            f"@ {self.logged_at:%Y-%m-%d}"
        )
