"""
src/backend/products/admin.py
==============================
Django Admin for the Gold product catalog.

Key Admin capabilities (CLAUDE.md §1):
  - Inspect and manually override entity resolution results
  - Flag low-confidence matches for human review
  - Monitor price history and stock availability across sites
  - DailyPriceLog is read-only (append-only TimescaleDB hypertable)
"""

from __future__ import annotations

from django.contrib import admin
from django.db.models import Avg, Count, Max, Min, QuerySet
from django.http import HttpRequest
from django.utils.html import format_html

from .models import DailyPriceLog, MasterProduct, SiteProduct


# ---------------------------------------------------------------------------
# Inline: SiteProduct inside MasterProduct detail
# ---------------------------------------------------------------------------

class SiteProductInline(admin.TabularInline):
    model = SiteProduct
    extra = 0
    readonly_fields = [
        "site",
        "raw_name",
        "raw_brand",
        "raw_ean",
        "current_price",
        "currency",
        "in_stock",
        "match_score",
        "product_url_link",
        "last_scraped_at",
    ]
    fields = readonly_fields
    show_change_link = True
    can_delete = False

    @admin.display(description="URL")
    def product_url_link(self, obj: SiteProduct) -> str:
        return format_html('<a href="{}" target="_blank">🔗 View</a>', obj.product_url)


# ---------------------------------------------------------------------------
# MasterProduct Admin
# ---------------------------------------------------------------------------

class MatchConfidenceFilter(admin.SimpleListFilter):
    title = "Match Confidence"
    parameter_name = "confidence_band"

    def lookups(self, request: HttpRequest, model_admin) -> list[tuple[str, str]]:
        return [
            ("high", "✅ High (≥ 0.95)"),
            ("medium", "🟡 Medium (0.85–0.94)"),
            ("low", "🔴 Low (< 0.85) — needs review"),
        ]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        if self.value() == "high":
            return queryset.filter(match_confidence__gte=0.95)
        if self.value() == "medium":
            return queryset.filter(match_confidence__gte=0.85, match_confidence__lt=0.95)
        if self.value() == "low":
            return queryset.filter(match_confidence__lt=0.85)
        return queryset


@admin.register(MasterProduct)
class MasterProductAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "brand",
        "ean",
        "status",
        "confidence_pill",
        "manually_verified",
        "site_count",
        "last_matched_at",
    ]
    list_filter = [
        "status",
        MatchConfidenceFilter,
        "manually_verified",
        "brand",
    ]
    search_fields = ["name", "brand", "ean", "mpn"]
    readonly_fields = ["id", "first_seen_at", "last_matched_at", "updated_at"]
    ordering = ["brand", "name"]
    inlines = [SiteProductInline]
    list_per_page = 100

    fieldsets = (
        (
            "Canonical Identity",
            {"fields": ("id", "name", "brand", "ean", "mpn", "status")},
        ),
        (
            "Classification",
            {
                "classes": ("collapse",),
                "fields": ("category", "subcategory", "tags"),
            },
        ),
        (
            "Content",
            {
                "classes": ("collapse",),
                "fields": ("description", "ingredients", "image_urls"),
            },
        ),
        (
            "Matching Metadata",
            {
                "fields": ("match_confidence", "manually_verified"),
                "description": (
                    "Scores below 0.85 are flagged by the matching pipeline. "
                    "Tick 'manually verified' after reviewing the site products below."
                ),
            },
        ),
        (
            "Timestamps",
            {
                "classes": ("collapse",),
                "fields": ("first_seen_at", "last_matched_at", "updated_at"),
            },
        ),
    )

    @admin.display(description="Confidence")
    def confidence_pill(self, obj: MasterProduct) -> str:
        score = obj.match_confidence
        colour = "#2ecc71" if score >= 0.95 else "#f39c12" if score >= 0.85 else "#e74c3c"
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-size:0.85em;">{:.0%}</span>',
            colour,
            score,
        )

    @admin.display(description="Sites")
    def site_count(self, obj: MasterProduct) -> int:
        return obj.site_products.count()

    @admin.action(description="Mark as manually verified")
    def verify_products(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(manually_verified=True)
        self.message_user(request, f"{updated} product(s) marked as verified.")

    @admin.action(description="Flag for review (set status = Under Review)")
    def flag_for_review(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(status=MasterProduct.Status.UNDER_REVIEW)
        self.message_user(request, f"{updated} product(s) flagged for review.")

    actions = ["verify_products", "flag_for_review"]


# ---------------------------------------------------------------------------
# SiteProduct Admin
# ---------------------------------------------------------------------------

@admin.register(SiteProduct)
class SiteProductAdmin(admin.ModelAdmin):
    list_display = [
        "raw_name",
        "site",
        "master_product_link",
        "current_price",
        "currency",
        "in_stock",
        "match_score_badge",
        "last_scraped_at",
    ]
    list_filter = ["site", "in_stock", "currency"]
    search_fields = ["raw_name", "raw_brand", "raw_ean", "product_url"]
    readonly_fields = [
        "id",
        "master_product",
        "site",
        "scraped_url",
        "match_score",
        "first_scraped_at",
        "last_scraped_at",
        "updated_at",
    ]
    ordering = ["site", "raw_name"]

    @admin.display(description="Master Product")
    def master_product_link(self, obj: SiteProduct) -> str:
        return format_html(
            '<a href="/admin/products/masterproduct/{}/change/">{}</a>',
            obj.master_product_id,
            str(obj.master_product)[:50],
        )

    @admin.display(description="Match")
    def match_score_badge(self, obj: SiteProduct) -> str:
        score = obj.match_score
        colour = "#2ecc71" if score >= 0.95 else "#f39c12" if score >= 0.85 else "#e74c3c"
        return format_html(
            '<span style="color:{};font-weight:bold;">{:.0%}</span>',
            colour,
            score,
        )


# ---------------------------------------------------------------------------
# DailyPriceLog Admin  (read-only — append-only TimescaleDB hypertable)
# ---------------------------------------------------------------------------

@admin.register(DailyPriceLog)
class DailyPriceLogAdmin(admin.ModelAdmin):
    list_display = [
        "logged_at",
        "master_product",
        "site",
        "price",
        "currency",
        "in_stock",
    ]
    list_filter = ["site", "in_stock", "currency"]
    search_fields = [
        "master_product__name",
        "site_product__raw_name",
    ]
    readonly_fields = [f.name for f in DailyPriceLog._meta.get_fields() if hasattr(f, "name")]
    ordering = ["-logged_at"]
    date_hierarchy = "logged_at"
    list_per_page = 200

    # Enforce append-only contract
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return False
