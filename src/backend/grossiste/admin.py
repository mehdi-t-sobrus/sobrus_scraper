"""
src/backend/grossiste/admin.py
"""
from __future__ import annotations

from django.contrib import admin, messages
from django.db.models import Count, Max, Min
from django.utils import timezone
from django.utils.html import format_html

from .models import GrossisteConfig, GrossisteOrder, GrossisteProduct


# ---------------------------------------------------------------------------
# GrossisteConfig Admin
# ---------------------------------------------------------------------------

@admin.register(GrossisteConfig)
class GrossisteConfigAdmin(admin.ModelAdmin):
    list_display = [
        "name", "domain", "sobrus_supplier_id", "is_active",
        "product_count", "matched_count", "last_sync_at",
    ]
    list_filter  = ["is_active"]
    readonly_fields = ["last_sync_at", "created_at", "updated_at"]
    fieldsets = (
        ("Identity", {"fields": ("name", "domain", "is_active")}),
        ("Sobrus Integration", {
            "fields": ("sobrus_supplier_id",),
            "description": (
                "The Sobrus internal supplier ID used in api.pharma.sobrus.com calls. "
                "GPM=1, Sophasais=1570, Lodimed=346. "
                "Credentials are NOT stored — they come from the user's Sobrus session cookie."
            ),
        }),
        ("API Paths (direct access)", {
            "classes": ("collapse",),
            "fields": ("login_path", "products_path", "order_path"),
        }),
        ("Sync", {"fields": ("last_sync_at", "created_at", "updated_at")}),
    )

    @admin.display(description="Products")
    def product_count(self, obj: GrossisteConfig) -> str:
        count    = obj.products.count()
        in_stock = obj.products.filter(in_stock=True).count()
        sobrus   = obj.products.filter(sobrus_product_id__isnull=False).count()
        if count == 0:
            return format_html('<span style="color:#e74c3c;">0 — not synced</span>')
        return format_html(
            '{} total <span style="color:#2ecc71;">({} in stock)</span> '
            '<span style="color:#0EA5E9;">({} have Sobrus ID)</span>',
            count, in_stock, sobrus,
        )

    @admin.display(description="Matched")
    def matched_count(self, obj: GrossisteConfig) -> str:
        total   = obj.products.count()
        matched = obj.products.filter(master_product__isnull=False).count()
        if total == 0:
            return "—"
        pct    = round(matched / total * 100)
        colour = "#2ecc71" if pct >= 80 else "#f39c12" if pct >= 50 else "#e74c3c"
        return format_html(
            '<span style="color:{};">{}/{} ({}%)</span>',
            colour, matched, total, pct,
        )


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class MatchedFilter(admin.SimpleListFilter):
    title          = "Master Product Link"
    parameter_name = "matched"

    def lookups(self, request, model_admin):
        return [
            ("yes",    "✅ Matched"),
            ("no",     "❌ Not matched"),
            ("review", "🔍 Needs review (low confidence)"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(master_product__isnull=False)
        if self.value() == "no":
            return queryset.filter(master_product__isnull=True)
        if self.value() == "review":
            return queryset.filter(
                master_product__isnull=False,
                match_confidence__lt=0.85,
                manually_verified=False,
            )
        return queryset


class HasSobrusIdFilter(admin.SimpleListFilter):
    title          = "Sobrus Product ID"
    parameter_name = "has_sobrus_id"

    def lookups(self, request, model_admin):
        return [
            ("yes", "✅ Has Sobrus ID"),
            ("no",  "❌ Missing Sobrus ID"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(sobrus_product_id__isnull=False)
        if self.value() == "no":
            return queryset.filter(sobrus_product_id__isnull=True)
        return queryset


# ---------------------------------------------------------------------------
# GrossisteProduct Admin
# ---------------------------------------------------------------------------

@admin.register(GrossisteProduct)
class GrossisteProductAdmin(admin.ModelAdmin):
    list_display = [
        "code", "name", "grossiste", "forme",
        "prix_pharmacien_col", "ppm_col",
        "stock_badge", "sobrus_id_col", "master_link", "margin_col",
        "wholesale_range_col",
        "availability_checked_at",
    ]
    list_filter   = [
        "grossiste", "in_stock", MatchedFilter,
        HasSobrusIdFilter, "manually_verified",
    ]
    search_fields = ["code", "name", "sobrus_product_id"]
    readonly_fields = [
        "code", "grossiste", "synced_at", "created_at",
        "in_stock", "availability_checked_at", "match_confidence",
        "pricing_panel",
    ]
    raw_id_fields   = ["master_product"]
    ordering        = ["name"]
    list_per_page   = 100
    list_select_related = ["grossiste", "master_product"]

    fieldsets = (
        ("Product", {"fields": ("grossiste", "code", "name", "forme")}),
        ("Pricing (MAD)", {"fields": ("prix_pharmacien", "ppm", "pa")}),
        ("Sobrus Integration", {
            "fields": ("sobrus_product_id",),
            "description": (
                "Required for availability checks and orders via api.pharma.sobrus.com. "
                "Populate via: POST /api/v1/grossiste/sync-sobrus-ids/"
            ),
        }),
        ("Master Product Link", {
            "fields": ("master_product", "match_confidence", "manually_verified"),
        }),
        ("📊 Pricing Intelligence", {"fields": ("pricing_panel",)}),
        ("Availability", {"fields": ("in_stock", "availability_checked_at")}),
        ("Timestamps", {"classes": ("collapse",), "fields": ("synced_at", "created_at")}),
    )

    # -- List columns --------------------------------------------------------

    @admin.display(description="Buy Price (MAD)", ordering="prix_pharmacien")
    def prix_pharmacien_col(self, obj: GrossisteProduct) -> str:
        if obj.prix_pharmacien is None:
            return "—"
        return format_html("<b>{}</b>", f"{float(obj.prix_pharmacien):.2f}")

    @admin.display(description="PPM (MAD)", ordering="ppm")
    def ppm_col(self, obj: GrossisteProduct) -> str:
        if obj.ppm is None:
            return "—"
        return format_html('<span style="color:#888;">{}</span>', f"{float(obj.ppm):.2f}")

    @admin.display(description="Stock")
    def stock_badge(self, obj: GrossisteProduct) -> str:
        if obj.in_stock is None:
            return format_html('<span style="color:#888;">⬜ ?</span>')
        if obj.in_stock:
            return format_html('<span style="color:#2ecc71;">✓</span>')
        return format_html('<span style="color:#e74c3c;">✗</span>')

    @admin.display(description="Sobrus ID")
    def sobrus_id_col(self, obj: GrossisteProduct) -> str:
        if obj.sobrus_product_id is None:
            return format_html('<span style="color:#e74c3c;">—</span>')
        return format_html(
            '<span style="color:#0EA5E9;">{}</span>', obj.sobrus_product_id
        )

    @admin.display(description="Master Product")
    def master_link(self, obj: GrossisteProduct) -> str:
        if not obj.master_product_id:
            return format_html('<span style="color:#e74c3c;">⚠ Not matched</span>')
        from django.urls import reverse
        url    = reverse("admin:products_masterproduct_change", args=[obj.master_product_id])
        conf   = obj.match_confidence
        colour = "#2ecc71" if (conf or 0) >= 0.85 else "#f39c12"
        return format_html(
            '<a href="{}" style="color:{};">{}</a>',
            url, colour, obj.master_product.name[:45],
        )

    @admin.display(description="Market Margin")
    def margin_col(self, obj: GrossisteProduct) -> str:
        if obj.prix_pharmacien is None or not obj.master_product_id:
            return "—"
        from products.models import SiteProduct
        cheapest = (
            SiteProduct.objects
            .filter(master_product_id=obj.master_product_id, in_stock=True,
                    current_price__isnull=False)
            .order_by("current_price")
            .values_list("current_price", flat=True)
            .first()
        )
        if cheapest is None:
            return "—"
        margin_pct = round(
            (float(cheapest) - float(obj.prix_pharmacien)) / float(obj.prix_pharmacien) * 100
        )
        if margin_pct >= 0:
            colour = "#2ecc71" if margin_pct >= 30 else "#f39c12" if margin_pct >= 10 else "#e74c3c"
            label  = f"+{margin_pct}%"
        else:
            colour = "#e74c3c"
            label  = f"{margin_pct}%"
        return format_html(
            '<span style="color:{};font-weight:bold;">{}</span>', colour, label,
        )

    @admin.display(description="Wholesale Range (MAD)")
    def wholesale_range_col(self, obj: GrossisteProduct) -> str:
        if not obj.master_product_id:
            return "—"
        stats = (
            GrossisteProduct.objects
            .filter(master_product_id=obj.master_product_id, prix_pharmacien__isnull=False)
            .aggregate(
                w_min=Min("prix_pharmacien"),
                w_max=Max("prix_pharmacien"),
                w_count=Count("grossiste", distinct=True),
            )
        )
        if stats["w_count"] is None or stats["w_min"] is None:
            return "—"
        if stats["w_count"] <= 1:
            return format_html(
                '<span style="color:#2ecc71;">{}</span> MAD',
                f"{float(stats['w_min']):.2f}",
            )
        return format_html(
            '<span style="color:#2ecc71;">{}</span>'
            ' – <span style="color:#e74c3c;">{}</span>'
            ' <span style="color:#888;font-size:0.85em;">({} grossistes)</span>',
            f"{float(stats['w_min']):.2f}",
            f"{float(stats['w_max']):.2f}",
            stats["w_count"],
        )

    # -- Detail panel --------------------------------------------------------

    @admin.display(description="Pricing Intelligence")
    def pricing_panel(self, obj: GrossisteProduct) -> str:
        if not obj.master_product_id:
            return format_html(
                '<p style="color:#999;">Link a Master Product above to see pricing analysis.</p>'
            )

        grossiste_products = list(
            GrossisteProduct.objects
            .filter(master_product_id=obj.master_product_id, prix_pharmacien__isnull=False)
            .select_related("grossiste")
            .order_by("prix_pharmacien")
        )

        from products.models import SiteProduct
        retail_products = list(
            SiteProduct.objects
            .filter(master_product_id=obj.master_product_id, current_price__isnull=False)
            .select_related("site")
            .order_by("current_price")
        )

        if not grossiste_products and not retail_products:
            return format_html('<p style="color:#999;">No pricing data available yet.</p>')

        w_prices = [float(p.prix_pharmacien) for p in grossiste_products]
        w_min    = min(w_prices) if w_prices else None

        r_prices = [float(p.current_price) for p in retail_products if p.in_stock]
        if not r_prices:
            r_prices = [float(p.current_price) for p in retail_products]
        r_min = min(r_prices) if r_prices else None

        margin_html = ""
        if w_min and r_min:
            margin_pct = round((r_min - w_min) / w_min * 100)
            margin_mad = round(r_min - w_min, 2)
            m_colour   = "#2ecc71" if margin_pct >= 30 else "#f39c12" if margin_pct >= 10 else "#e74c3c"
            sign       = "+" if margin_pct >= 0 else ""
            margin_html = format_html(
                '<div style="background:#0A2218;border-radius:6px;padding:10px 14px;'
                'margin-bottom:14px;display:flex;gap:32px;align-items:center;">'
                '<div><div style="font-size:0.75em;color:#7AAFC0;text-transform:uppercase;">Best buy price</div>'
                '<div style="font-size:1.3em;font-weight:700;color:#2ecc71;">{} MAD</div>'
                '<div style="font-size:0.8em;color:#7AAFC0;">from {}</div></div>'
                '<div style="font-size:1.5em;color:#444;">→</div>'
                '<div><div style="font-size:0.75em;color:#7AAFC0;text-transform:uppercase;">Cheapest retail</div>'
                '<div style="font-size:1.3em;font-weight:700;color:#e74c3c;">{} MAD</div></div>'
                '<div style="margin-left:auto;text-align:right;">'
                '<div style="font-size:0.75em;color:#7AAFC0;text-transform:uppercase;">Margin</div>'
                '<div style="font-size:1.6em;font-weight:700;color:{};">{}{}%</div>'
                '<div style="font-size:0.8em;color:#7AAFC0;">+{} MAD per unit</div>'
                '</div></div>',
                f"{w_min:.2f}",
                grossiste_products[0].grossiste.name,
                f"{r_min:.2f}",
                m_colour, sign, margin_pct, margin_mad,
            )

        # Wholesale table
        w_rows = []
        for i, gp in enumerate(grossiste_products):
            is_best = (i == 0)
            w_rows.append(format_html(
                '<tr style="background:{};">'
                '<td style="padding:8px 10px;font-weight:{};">{}{}</td>'
                '<td style="padding:8px 10px;font-weight:bold;color:{};">{} MAD</td>'
                '<td style="padding:8px 10px;color:#888;">{}</td>'
                '<td style="padding:8px 10px;color:#0EA5E9;">{}</td>'
                '<td style="padding:8px 10px;">{}</td>'
                '</tr>',
                "#0A2218" if is_best else "#0D1F2D",
                "bold" if is_best else "normal",
                "🏆 " if is_best else "", gp.grossiste.name,
                "#2ecc71" if is_best else "#e0e0e0",
                f"{float(gp.prix_pharmacien):.2f}",
                f"{float(gp.ppm):.2f}" if gp.ppm else "—",
                str(gp.sobrus_product_id) if gp.sobrus_product_id else "—",
                format_html('<span style="color:#2ecc71;">✓</span>') if gp.in_stock else
                format_html('<span style="color:#e74c3c;">✗</span>') if gp.in_stock is False else
                format_html('<span style="color:#888;">?</span>'),
            ))

        wholesale_table = format_html(
            '<h4 style="color:#2ecc71;margin:0 0 8px;font-size:0.85em;'
            'text-transform:uppercase;">🏭 Wholesale Prices</h4>'
            '<table style="width:100%;border-collapse:collapse;margin-bottom:16px;">'
            '<thead><tr style="background:#162032;">'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Grossiste</th>'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Buy Price</th>'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">PPM</th>'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Sobrus ID</th>'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Stock</th>'
            '</tr></thead><tbody>{}</tbody></table>',
            format_html("".join(str(r) for r in w_rows)) if w_rows
            else format_html('<tr><td colspan="5" style="padding:8px;color:#888;">No wholesale data</td></tr>'),
        )

        # Retail table
        r_rows = []
        for i, sp in enumerate(retail_products):
            is_cheapest = (i == 0 and sp.in_stock)
            r_rows.append(format_html(
                '<tr style="background:{};">'
                '<td style="padding:8px 10px;font-weight:{};">{}{}</td>'
                '<td style="padding:8px 10px;font-weight:bold;color:{};">{} MAD</td>'
                '<td style="padding:8px 10px;">{}</td>'
                '<td style="padding:8px 10px;">'
                '<a href="{}" target="_blank" style="color:#185FA5;font-size:0.85em;">View →</a>'
                '</td></tr>',
                "#0A1A2A" if is_cheapest else "#0D1F2D",
                "bold" if is_cheapest else "normal",
                "🏆 " if is_cheapest else "", sp.site.domain,
                "#2ecc71" if is_cheapest else "#e0e0e0",
                f"{float(sp.current_price):.2f}",
                format_html('<span style="color:#2ecc71;font-size:0.85em;">✓ In Stock</span>')
                if sp.in_stock else
                format_html('<span style="color:#e74c3c;font-size:0.85em;">✗ Out of Stock</span>'),
                sp.product_url,
            ))

        retail_table = format_html(
            '<h4 style="color:#e74c3c;margin:0 0 8px;font-size:0.85em;'
            'text-transform:uppercase;">🛒 Retail Market Prices</h4>'
            '<table style="width:100%;border-collapse:collapse;">'
            '<thead><tr style="background:#162032;">'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Site</th>'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Retail Price</th>'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Stock</th>'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Link</th>'
            '</tr></thead><tbody>{}</tbody></table>',
            format_html("".join(str(r) for r in r_rows)) if r_rows
            else format_html('<tr><td colspan="4" style="padding:8px;color:#888;">No retail data yet</td></tr>'),
        )

        return format_html(
            '<div style="background:#0D1F2D;border-radius:8px;padding:16px;'
            'font-family:inherit;max-width:900px;">{}{}{}</div>',
            margin_html, wholesale_table, retail_table,
        )

    @admin.action(description="✅ Mark master product link as verified")
    def verify_match(self, request, queryset):
        updated = queryset.update(manually_verified=True)
        messages.success(request, f"{updated} product(s) marked as verified.")

    @admin.action(description="🛒 Create draft order (qty=1)")
    def create_draft_order(self, request, queryset):
        for product in queryset.select_related("grossiste"):
            order = GrossisteOrder.objects.create(
                grossiste=product.grossiste,
                product=product,
                quantity=1,
                unit_price=product.prix_pharmacien,
                sale_price=product.ppm,
                status=GrossisteOrder.Status.DRAFT,
            )
            messages.info(
                request,
                f"Draft order #{order.pk} created for {product.name}. "
                f"Submit via POST /api/v1/grossiste/order/ with your Sobrus cookie.",
            )

    @admin.display(description="ℹ️ Availability")
    def availability_note(self, obj):
        return format_html(
            '<span style="color:#888;">Use POST /api/v1/grossiste/check-availability/ '
            'with your Sobrus cookie</span>'
        )

    actions = ["verify_match", "create_draft_order"]


# ---------------------------------------------------------------------------
# GrossisteOrder Admin
# ---------------------------------------------------------------------------

@admin.register(GrossisteOrder)
class GrossisteOrderAdmin(admin.ModelAdmin):
    list_display = [
        "id", "sobrus_transaction_num", "grossiste", "product_name",
        "quantity", "unit_price", "total_col",
        "status_badge", "sobrus_status", "created_at",
    ]
    list_filter   = ["status", "grossiste"]
    search_fields = [
        "product__name", "product__code",
        "sobrus_order_id", "sobrus_transaction_num",
    ]
    readonly_fields = [
        "grossiste", "product", "unit_price", "sale_price",
        "sobrus_order_id", "sobrus_transaction_num", "sobrus_status",
        "response_payload", "submitted_at", "created_at", "updated_at",
    ]
    ordering = ["-created_at"]
    list_select_related = ["grossiste", "product"]

    fieldsets = (
        ("Order", {
            "fields": ("grossiste", "product", "quantity", "unit_price", "sale_price", "status", "notes"),
        }),
        ("Sobrus", {
            "fields": ("sobrus_order_id", "sobrus_transaction_num", "sobrus_status",
                       "submitted_at", "response_payload"),
        }),
        ("Error", {
            "classes": ("collapse",),
            "fields": ("error_message",),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

    @admin.display(description="Product")
    def product_name(self, obj: GrossisteOrder) -> str:
        if obj.product:
            return f"{obj.product.code} — {obj.product.name}"
        return "—"

    @admin.display(description="Total (MAD)")
    def total_col(self, obj: GrossisteOrder) -> str:
        total = obj.total_price
        if total is None:
            return "—"
        return format_html("<b>{:.2f}</b>", float(total))

    @admin.display(description="Status")
    def status_badge(self, obj: GrossisteOrder) -> str:
        colours = {
            "draft":     ("#888",    "⬜"),
            "submitted": ("#0EA5E9", "📤"),
            "confirmed": ("#2ecc71", "✅"),
            "failed":    ("#e74c3c", "❌"),
        }
        colour, icon = colours.get(obj.status, ("#888", "?"))
        return format_html(
            '<span style="color:{};font-weight:bold;">{} {}</span>',
            colour, icon, obj.get_status_display(),
        )

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status != GrossisteOrder.Status.DRAFT:
            return False
        return True