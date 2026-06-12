"""
src/backend/products/admin.py
==============================
Django Admin for the Gold product catalog.
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
        "image_thumb",
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
    show_change_link = False
    can_delete = False

    @admin.display(description="Image")
    def image_thumb(self, obj: SiteProduct) -> str:
        if obj.image_url:
            return format_html(
                '<img src="{}" style="height:48px;width:48px;object-fit:contain;'
                'border-radius:4px;background:#f5f5f5;" loading="lazy">',
                obj.image_url,
            )
        return "—"

    @admin.display(description="URL")
    def product_url_link(self, obj: SiteProduct) -> str:
        return format_html(
            '<a href="{}" target="_blank" style="white-space:nowrap;">🔗 View on site</a>',
            obj.product_url,
        )


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class MatchConfidenceFilter(admin.SimpleListFilter):
    title = "Match Confidence"
    parameter_name = "confidence_band"

    def lookups(self, request, model_admin):
        return [
            ("high",   "✅ High (≥ 0.95)"),
            ("medium", "🟡 Medium (0.85–0.94)"),
            ("low",    "🔴 Low (< 0.85) — needs review"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "high":
            return queryset.filter(match_confidence__gte=0.95)
        if self.value() == "medium":
            return queryset.filter(match_confidence__gte=0.85, match_confidence__lt=0.95)
        if self.value() == "low":
            return queryset.filter(match_confidence__lt=0.85)
        return queryset


class SiteCountFilter(admin.SimpleListFilter):
    title = "Sites Coverage"
    parameter_name = "site_coverage"

    def lookups(self, request, model_admin):
        return [
            ("multi",  "🌐 On multiple sites (2+)"),
            ("single", "📌 On one site only"),
            ("none",   "⚠️ No sites (orphaned)"),
        ]

    def queryset(self, request, queryset):
        qs = queryset.annotate(_sc=Count("site_products", distinct=True))
        if self.value() == "multi":
            return qs.filter(_sc__gte=2)
        if self.value() == "single":
            return qs.filter(_sc=1)
        if self.value() == "none":
            return qs.filter(_sc=0)
        return queryset


# ---------------------------------------------------------------------------
# MasterProduct Admin
# ---------------------------------------------------------------------------

@admin.register(MasterProduct)
class MasterProductAdmin(admin.ModelAdmin):
    list_display = [
        "primary_image",
        "name",
        "brand",
        "ean",
        "status",
        "confidence_pill",
        "site_count",
        "price_range",
        "cheapest_site",
        "last_matched_at",
    ]
    list_display_links = ["primary_image", "name"]   # both image and name are clickable
    list_filter = [
        "status",
        SiteCountFilter,
        MatchConfidenceFilter,
        "manually_verified",
    ]
    search_fields = ["name", "brand", "ean", "mpn"]
    readonly_fields = [
        "id", "first_seen_at", "last_matched_at", "updated_at",
        "price_comparison_panel",
    ]
    ordering = ["brand", "name"]
    inlines = [SiteProductInline]
    list_per_page = 50
    list_select_related = False

    # Default to showing only products that appear on at least one site
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Annotate with site count for filters and display
        return qs.annotate(_site_count=Count("site_products", distinct=True))

    fieldsets = (
        (
            "Canonical Identity",
            {"fields": ("id", "name", "brand", "ean", "mpn", "status")},
        ),
        (
            "💰 Price Comparison",
            {
                "fields": ("price_comparison_panel",),
                "description": "Current prices across all sites. Sorted cheapest first.",
            },
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

    # -- List display columns ------------------------------------------------

    @admin.display(description="")
    def primary_image(self, obj: MasterProduct) -> str:
        if obj.image_urls:
            return format_html(
                '<img src="{}" style="height:40px;width:40px;object-fit:contain;'
                'border-radius:4px;background:#f5f5f5;" loading="lazy">',
                obj.image_urls[0],
            )
        return format_html(
            '<div style="height:40px;width:40px;background:#f0f0f0;border-radius:4px;'
            'display:flex;align-items:center;justify-content:center;color:#bbb;'
            'font-size:18px;">📦</div>'
        )

    @admin.display(description="Confidence")
    def confidence_pill(self, obj: MasterProduct) -> str:
        score = obj.match_confidence
        colour = "#2ecc71" if score >= 0.95 else "#f39c12" if score >= 0.85 else "#e74c3c"
        pct = f"{score:.0%}"
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:10px;font-size:0.85em;">{}</span>',
            colour, pct,
        )

    @admin.display(description="Sites")
    def site_count(self, obj: MasterProduct) -> str:
        count = getattr(obj, "_site_count", obj.site_products.count())
        if count == 0:
            return format_html('<span style="color:#e74c3c;">⚠️ 0</span>')
        if count >= 2:
            return format_html('<span style="color:#2ecc71;font-weight:bold;">🌐 {}</span>', count)
        return str(count)

    @admin.display(description="Price Range (MAD)")
    def price_range(self, obj: MasterProduct) -> str:
        stats = obj.site_products.filter(
            current_price__isnull=False
        ).aggregate(
            min_price=Min("current_price"),
            max_price=Max("current_price"),
        )
        p_min = stats["min_price"]
        p_max = stats["max_price"]
        if p_min is None:
            return "—"
        if p_min == p_max:
            return format_html("<b>{}</b> MAD", f"{p_min:.0f}")
        saving_pct = round((1 - p_min / p_max) * 100)
        return format_html(
            '<span style="color:#2ecc71;font-weight:bold;">{}</span>'
            ' → <span style="color:#e74c3c">{}</span>'
            ' <span style="color:#888;font-size:0.85em;">(-{}%)</span>',
            f"{p_min:.0f}", f"{p_max:.0f}", saving_pct,
        )

    @admin.display(description="Cheapest Site")
    def cheapest_site(self, obj: MasterProduct) -> str:
        sp = (
            obj.site_products
            .filter(current_price__isnull=False, in_stock=True)
            .select_related("site")
            .order_by("current_price")
            .first()
        )
        if not sp:
            sp = (
                obj.site_products
                .filter(current_price__isnull=False)
                .select_related("site")
                .order_by("current_price")
                .first()
            )
        if not sp:
            return "—"
        return format_html(
            '<a href="{}" target="_blank" style="color:#185FA5;">{}</a>',
            sp.product_url, sp.site.domain,
        )

    # -- Detail page panel ---------------------------------------------------

    @admin.display(description="Price Comparison")
    def price_comparison_panel(self, obj: MasterProduct) -> str:
        site_products = list(
            obj.site_products
            .filter(current_price__isnull=False)
            .select_related("site")
            .order_by("current_price")
        )

        if not site_products:
            return format_html(
                '<p style="color:#999;padding:8px 0;">'
                'No price data yet — run the matching pipeline first.</p>'
            )

        # Aggregates — prefer in-stock prices
        in_stock = [sp for sp in site_products if sp.in_stock]
        pool     = in_stock if in_stock else site_products
        p_min    = float(pool[0].current_price)
        p_max    = float(pool[-1].current_price)
        p_avg    = sum(float(sp.current_price) for sp in pool) / len(pool)
        saving   = round((1 - p_min / p_max) * 100) if p_max else 0
        currency = pool[0].currency

        # Summary bar
        summary = format_html(
            '<div style="display:flex;gap:28px;padding:14px 16px;margin-bottom:16px;'
            'background:#f8fffe;border:1px solid #d4edda;border-radius:8px;'
            'flex-wrap:wrap;">'
            '<div><div style="font-size:0.75em;color:#666;text-transform:uppercase;'
            'letter-spacing:0.05em;">Cheapest</div>'
            '<div style="font-size:1.4em;font-weight:700;color:#2ecc71;">'
            '{min} {cur}</div></div>'
            '<div><div style="font-size:0.75em;color:#666;text-transform:uppercase;'
            'letter-spacing:0.05em;">Average</div>'
            '<div style="font-size:1.4em;font-weight:500;color:#333;">'
            '{avg} {cur}</div></div>'
            '<div><div style="font-size:0.75em;color:#666;text-transform:uppercase;'
            'letter-spacing:0.05em;">Most Expensive</div>'
            '<div style="font-size:1.4em;font-weight:500;color:#e74c3c;">'
            '{max} {cur}</div></div>'
            '<div><div style="font-size:0.75em;color:#666;text-transform:uppercase;'
            'letter-spacing:0.05em;">Max Saving</div>'
            '<div style="font-size:1.4em;font-weight:700;color:#185FA5;">'
            '{saving}%</div></div>'
            '</div>',
            min=f"{p_min:.0f}", avg=f"{p_avg:.0f}",
            max=f"{p_max:.0f}", cur=currency, saving=saving,
        )

        # Per-site table — sorted: in-stock cheapest first
        sorted_sps = sorted(
            site_products,
            key=lambda x: (0 if x.in_stock else 1, float(x.current_price or 9999))
        )

        rows = []
        for i, sp in enumerate(sorted_sps):
            is_cheapest = (i == 0 and sp.in_stock)
            row_bg = "#f0fff4" if is_cheapest else "#fff8f8" if not sp.in_stock else "#ffffff"
            badge  = format_html(
                ' <span style="background:#2ecc71;color:#fff;padding:1px 6px;'
                'border-radius:8px;font-size:0.75em;vertical-align:middle;">🏆 Cheapest</span>'
            ) if is_cheapest else ""

            img_html = format_html(
                '<img src="{}" style="height:52px;width:52px;object-fit:contain;'
                'border-radius:4px;background:#f5f5f5;" loading="lazy">',
                sp.image_url,
            ) if sp.image_url else format_html(
                '<div style="height:52px;width:52px;background:#f0f0f0;border-radius:4px;'
                'display:flex;align-items:center;justify-content:center;color:#bbb;">📦</div>'
            )

            stock_badge = format_html(
                '<span style="background:{bg};color:{fg};padding:2px 8px;'
                'border-radius:10px;font-size:0.8em;">{label}</span>',
                bg="#d4edda" if sp.in_stock else "#f8d7da",
                fg="#155724" if sp.in_stock else "#721c24",
                label="✓ In Stock" if sp.in_stock else "✗ Out of Stock",
            )

            rows.append(format_html(
                '<tr style="background:{bg};border-bottom:1px solid #f0f0f0;">'
                '<td style="padding:12px 8px;width:60px;">{img}</td>'
                '<td style="padding:12px 8px;">'
                '  <div style="font-weight:600;font-size:0.95em;">{domain}{badge}</div>'
                '  <div style="color:#666;font-size:0.82em;margin-top:2px;">{name}</div>'
                '</td>'
                '<td style="padding:12px 8px;font-size:1.15em;font-weight:700;'
                'color:{price_color};white-space:nowrap;">{price} {cur}</td>'
                '<td style="padding:12px 8px;">{stock}</td>'
                '<td style="padding:12px 8px;">'
                '  <a href="{url}" target="_blank" style="background:#185FA5;color:#fff;'
                '  padding:5px 12px;border-radius:4px;text-decoration:none;font-size:0.85em;'
                '  white-space:nowrap;">View →</a>'
                '</td>'
                '</tr>',
                bg=row_bg,
                img=img_html,
                domain=sp.site.domain,
                badge=badge,
                name=sp.raw_name[:60] + "..." if len(sp.raw_name) > 60 else sp.raw_name,
                price=f"{float(sp.current_price):.0f}",
                price_color="#2ecc71" if is_cheapest else "#333",
                cur=sp.currency,
                stock=stock_badge,
                url=sp.product_url,
            ))

        table = format_html(
            '<table style="width:100%;border-collapse:collapse;">'
            '<thead><tr style="background:#f8f9fa;border-bottom:2px solid #dee2e6;">'
            '<th style="padding:10px 8px;text-align:left;font-size:0.85em;color:#666;'
            'text-transform:uppercase;letter-spacing:0.05em;width:60px;"></th>'
            '<th style="padding:10px 8px;text-align:left;font-size:0.85em;color:#666;'
            'text-transform:uppercase;letter-spacing:0.05em;">Site</th>'
            '<th style="padding:10px 8px;text-align:left;font-size:0.85em;color:#666;'
            'text-transform:uppercase;letter-spacing:0.05em;">Price</th>'
            '<th style="padding:10px 8px;text-align:left;font-size:0.85em;color:#666;'
            'text-transform:uppercase;letter-spacing:0.05em;">Stock</th>'
            '<th style="padding:10px 8px;text-align:left;font-size:0.85em;color:#666;'
            'text-transform:uppercase;letter-spacing:0.05em;">Link</th>'
            '</tr></thead>'
            '<tbody>{rows}</tbody>'
            '</table>',
            rows=format_html("".join(str(r) for r in rows)),
        )

        return format_html(
            '<div style="font-family:inherit;max-width:900px;">{summary}{table}</div>',
            summary=summary, table=table,
        )

    # -- Actions -------------------------------------------------------------

    @admin.action(description="✅ Mark as manually verified")
    def verify_products(self, request, queryset):
        updated = queryset.update(manually_verified=True)
        self.message_user(request, f"{updated} product(s) marked as verified.")

    @admin.action(description="🔍 Flag for review")
    def flag_for_review(self, request, queryset):
        updated = queryset.update(status=MasterProduct.Status.UNDER_REVIEW)
        self.message_user(request, f"{updated} product(s) flagged for review.")

    @admin.action(description="🗑️ Delete orphaned masters (0 site products)")
    def delete_orphans(self, request, queryset):
        orphans = queryset.annotate(
            _sc=Count("site_products", distinct=True)
        ).filter(_sc=0)
        count = orphans.count()
        orphans.delete()
        self.message_user(request, f"Deleted {count} orphaned MasterProduct(s) with no site listings.")

    actions = ["verify_products", "flag_for_review", "delete_orphans"]


# ---------------------------------------------------------------------------
# SiteProduct Admin (standalone)
# ---------------------------------------------------------------------------

@admin.register(SiteProduct)
class SiteProductAdmin(admin.ModelAdmin):
    list_display = [
        "image_thumb",
        "raw_name",
        "raw_brand",
        "site",
        "current_price",
        "currency",
        "in_stock",
        "match_score_badge",
        "master_link",
        "last_scraped_at",
    ]
    list_filter = ["site", "in_stock"]
    search_fields = ["raw_name", "raw_brand", "raw_ean", "product_url"]
    readonly_fields = [
        "id", "master_product", "site", "scraped_url",
        "first_scraped_at", "last_scraped_at", "updated_at",
    ]
    ordering = ["-last_scraped_at"]
    list_per_page = 100

    @admin.display(description="Image")
    def image_thumb(self, obj: SiteProduct) -> str:
        if obj.image_url:
            return format_html(
                '<img src="{}" style="height:40px;width:40px;object-fit:contain;'
                'border-radius:3px;background:#f5f5f5;" loading="lazy">',
                obj.image_url,
            )
        return "—"

    @admin.display(description="Match")
    def match_score_badge(self, obj: SiteProduct) -> str:
        score = obj.match_score
        colour = "#2ecc71" if score >= 0.95 else "#f39c12" if score >= 0.85 else "#e74c3c"
        pct = f"{score:.0%}"
        return format_html('<span style="color:{};font-weight:bold;">{}</span>', colour, pct)

    @admin.display(description="Master Product")
    def master_link(self, obj: SiteProduct) -> str:
        if obj.master_product_id:
            from django.urls import reverse
            url = reverse("admin:products_masterproduct_change", args=[obj.master_product_id])
            return format_html(
                '<a href="{}" style="color:#185FA5;">{}</a>',
                url, str(obj.master_product)[:40],
            )
        return "—"


# ---------------------------------------------------------------------------
# DailyPriceLog Admin (read-only)
# ---------------------------------------------------------------------------

@admin.register(DailyPriceLog)
class DailyPriceLogAdmin(admin.ModelAdmin):
    list_display = [
        "master_product", "site", "price", "currency", "in_stock", "logged_at"
    ]
    list_filter  = ["site", "in_stock"]
    search_fields = ["master_product__name", "master_product__brand"]
    ordering = ["-logged_at"]
    list_per_page = 200

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Price logs are immutable historical records — never delete directly.
        # Deleting a MasterProduct/SiteProduct now sets the FK to NULL (SET_NULL)
        # rather than cascading, so this lock no longer blocks orphan cleanup.
        return False
