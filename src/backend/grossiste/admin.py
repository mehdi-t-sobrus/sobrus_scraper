"""
src/backend/grossiste/admin.py
================================
Django Admin for the grossiste wholesale integration.

Key feature: MasterProduct detail page now shows a third panel alongside
the e-commerce price comparison — wholesale prices from grossistes, with
margin calculation between wholesale cost and retail market price.
"""

from __future__ import annotations

import asyncio

from django.contrib import admin, messages
from django.db.models import Avg, Count, Max, Min
from django.utils import timezone
from django.utils.html import format_html

from .client import GrossisteAPIError, GrossisteAuthError, GrossisteClient
from .models import GrossisteConfig, GrossisteOrder, GrossisteProduct


# ---------------------------------------------------------------------------
# GrossisteConfig Admin
# ---------------------------------------------------------------------------

@admin.register(GrossisteConfig)
class GrossisteConfigAdmin(admin.ModelAdmin):
    list_display = [
        "name", "domain", "is_active",
        "product_count", "matched_count", "last_sync_at",
    ]
    list_filter  = ["is_active"]
    readonly_fields = ["last_sync_at", "created_at", "updated_at"]
    fieldsets = (
        ("Identity", {"fields": ("name", "domain", "is_active")}),
        ("API Paths", {
            "classes": ("collapse",),
            "fields": ("login_path", "products_path", "order_path"),
            "description": (
                "Override only if this distributor uses non-standard paths. "
                "Credentials are NOT stored here — they are passed per-request from the ERP."
            ),
        }),
        ("Sync", {"fields": ("last_sync_at", "created_at", "updated_at")}),
    )

    @admin.display(description="Products")
    def product_count(self, obj: GrossisteConfig) -> str:
        count    = obj.products.count()
        in_stock = obj.products.filter(in_stock=True).count()
        if count == 0:
            return format_html('<span style="color:#e74c3c;">0 — not synced</span>')
        return format_html(
            '{} total <span style="color:#2ecc71;">({} in stock)</span>',
            count, in_stock,
        )

    @admin.display(description="Matched")
    def matched_count(self, obj: GrossisteConfig) -> str:
        total   = obj.products.count()
        matched = obj.products.filter(master_product__isnull=False).count()
        if total == 0:
            return "—"
        pct = round(matched / total * 100)
        colour = "#2ecc71" if pct >= 80 else "#f39c12" if pct >= 50 else "#e74c3c"
        return format_html(
            '<span style="color:{};">{}/{} ({}%)</span>',
            colour, matched, total, pct,
        )

    @admin.action(description="🔄 Sync product catalogue from grossiste")
    def sync_catalogue(self, request, queryset):
        for config in queryset:
            try:
                result = asyncio.run(_sync_catalogue(config))
                config.last_sync_at = timezone.now()
                config.save(update_fields=["last_sync_at"])
                messages.success(
                    request,
                    f"[{config.name}] {result['created']} new + "
                    f"{result['updated']} updated products.",
                )
            except (GrossisteAuthError, GrossisteAPIError) as exc:
                messages.error(request, f"[{config.name}] Sync failed: {exc}")

    actions = ["sync_catalogue"]


# ---------------------------------------------------------------------------
# GrossisteProduct Admin
# ---------------------------------------------------------------------------

class MatchedFilter(admin.SimpleListFilter):
    title        = "Master Product Link"
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


@admin.register(GrossisteProduct)
class GrossisteProductAdmin(admin.ModelAdmin):
    list_display = [
        "code", "name", "grossiste", "forme",
        "prix_pharmacien_col", "ppm_col",
        "stock_badge", "master_link", "margin_col",
        "wholesale_range_col",
        "availability_checked_at",
    ]
    list_filter   = ["grossiste", "in_stock", MatchedFilter, "manually_verified"]
    search_fields = ["code", "name"]
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
        ("Master Product Link", {
            "fields": ("master_product", "match_confidence", "manually_verified"),
            "description": (
                "Link to the canonical MasterProduct. "
                "Set by the matching pipeline — override manually if wrong."
            ),
        }),
        ("📊 Pricing Intelligence", {
            "fields": ("pricing_panel",),
            "description": "Wholesale vs retail market comparison.",
        }),
        ("Availability", {"fields": ("in_stock", "availability_checked_at")}),
        ("Timestamps", {"classes": ("collapse",), "fields": ("synced_at", "created_at")}),
    )

    # -- List columns --------------------------------------------------------

    @admin.display(description="Buy Price (MAD)", ordering="prix_pharmacien")
    def prix_pharmacien_col(self, obj: GrossisteProduct) -> str:
        if obj.prix_pharmacien is None:
            return "—"
        return format_html("<b>{}</b>", f"{float(obj.prix_pharmacien):.2f}")

    @admin.display(description="Max Retail (MAD)", ordering="ppm")
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

    @admin.display(description="Master Product")
    def master_link(self, obj: GrossisteProduct) -> str:
        if not obj.master_product_id:
            return format_html('<span style="color:#e74c3c;">⚠ Not matched</span>')
        from django.urls import reverse
        url = reverse("admin:products_masterproduct_change", args=[obj.master_product_id])
        conf = obj.match_confidence
        colour = "#2ecc71" if (conf or 0) >= 0.85 else "#f39c12"
        return format_html(
            '<a href="{}" style="color:{};">{}</a>',
            url,
            colour,
            obj.master_product.name[:45],
        )

    @admin.display(description="Wholesale Range (MAD)")
    def wholesale_range_col(self, obj: GrossisteProduct) -> str:
        """Min/max buy price for this product across all grossistes."""
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
            # Only one grossiste — just show the price, no range
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

    @admin.display(description="Market Margin")
    def margin_col(self, obj: GrossisteProduct) -> str:
        """Show margin between this grossiste price and cheapest retail price."""
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
            '<span style="color:{};font-weight:bold;">{}</span>',
            colour, label,
        )

    # -- Detail panel --------------------------------------------------------

    @admin.display(description="Pricing Intelligence")
    def pricing_panel(self, obj: GrossisteProduct) -> str:
        if not obj.master_product_id:
            return format_html(
                '<p style="color:#999;">Link a Master Product above to see pricing analysis.</p>'
            )

        # Wholesale prices — all grossistes for this master product
        grossiste_products = list(
            GrossisteProduct.objects
            .filter(master_product_id=obj.master_product_id, prix_pharmacien__isnull=False)
            .select_related("grossiste")
            .order_by("prix_pharmacien")
        )

        # Retail prices — e-commerce sites
        from products.models import SiteProduct
        retail_products = list(
            SiteProduct.objects
            .filter(master_product_id=obj.master_product_id, current_price__isnull=False)
            .select_related("site")
            .order_by("current_price")
        )

        if not grossiste_products and not retail_products:
            return format_html('<p style="color:#999;">No pricing data available yet.</p>')

        # ── Wholesale summary ──
        w_prices = [float(p.prix_pharmacien) for p in grossiste_products]
        w_min    = min(w_prices) if w_prices else None
        w_max    = max(w_prices) if w_prices else None

        # ── Retail summary ──
        r_prices = [float(p.current_price) for p in retail_products if p.in_stock]
        if not r_prices:
            r_prices = [float(p.current_price) for p in retail_products]
        r_min = min(r_prices) if r_prices else None
        r_max = max(r_prices) if r_prices else None
        r_avg = sum(r_prices) / len(r_prices) if r_prices else None

        # ── Margin calculation ──
        margin_html = ""
        if w_min and r_min:
            margin_pct  = round((r_min - w_min) / w_min * 100)
            margin_mad  = round(r_min - w_min, 2)
            m_colour    = "#2ecc71" if margin_pct >= 30 else "#f39c12" if margin_pct >= 10 else "#e74c3c"
            margin_html = format_html(
                '<div style="background:#0A2218;border-radius:6px;padding:10px 14px;'
                'margin-bottom:14px;display:flex;gap:32px;align-items:center;">'
                '<div><div style="font-size:0.75em;color:#7AAFC0;text-transform:uppercase;">'
                'Best buy price</div>'
                '<div style="font-size:1.3em;font-weight:700;color:#2ecc71;">{w_min} MAD</div>'
                '<div style="font-size:0.8em;color:#7AAFC0;">from {grossiste}</div></div>'
                '<div style="font-size:1.5em;color:#444;">→</div>'
                '<div><div style="font-size:0.75em;color:#7AAFC0;text-transform:uppercase;">'
                'Cheapest retail</div>'
                '<div style="font-size:1.3em;font-weight:700;color:#e74c3c;">{r_min} MAD</div>'
                '<div style="font-size:0.8em;color:#7AAFC0;">market price</div></div>'
                '<div style="margin-left:auto;text-align:right;">'
                '<div style="font-size:0.75em;color:#7AAFC0;text-transform:uppercase;">'
                'Margin</div>'
                '<div style="font-size:1.6em;font-weight:700;color:{colour};">+{pct}%</div>'
                '<div style="font-size:0.8em;color:#7AAFC0;">+{mad} MAD per unit</div>'
                '</div></div>',
                w_min=f"{w_min:.2f}",
                grossiste=grossiste_products[0].grossiste.name,
                r_min=f"{r_min:.2f}",
                colour=m_colour,
                pct=margin_pct,
                mad=margin_mad,
            )

        # ── Wholesale table ──
        w_rows = []
        for i, gp in enumerate(grossiste_products):
            is_best = (i == 0)
            w_rows.append(format_html(
                '<tr style="background:{};">'
                '<td style="padding:8px 10px;font-weight:{};">{}</td>'
                '<td style="padding:8px 10px;font-weight:bold;color:{};">{:.2f} MAD</td>'
                '<td style="padding:8px 10px;color:#888;">{:.2f} MAD</td>'
                '<td style="padding:8px 10px;">{}</td>'
                '</tr>',
                "#0A2218" if is_best else "#0D1F2D",
                "bold" if is_best else "normal",
                ("🏆 " if is_best else "") + gp.grossiste.name,
                "#2ecc71" if is_best else "#e0e0e0",
                float(gp.prix_pharmacien),
                float(gp.ppm or 0),
                format_html('<span style="color:{};">✓</span>', "#2ecc71")
                if gp.in_stock else
                format_html('<span style="color:{};">✗</span>', "#e74c3c")
                if gp.in_stock is False else
                format_html('<span style="color:#888;">?</span>'),
            ))

        wholesale_table = format_html(
            '<h4 style="color:#2ecc71;margin:0 0 8px;font-size:0.85em;'
            'text-transform:uppercase;letter-spacing:0.05em;">🏭 Wholesale Prices</h4>'
            '<table style="width:100%;border-collapse:collapse;margin-bottom:16px;">'
            '<thead><tr style="background:#162032;">'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Grossiste</th>'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Buy Price</th>'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">PPM</th>'
            '<th style="padding:8px 10px;text-align:left;color:#7AAFC0;font-size:0.8em;">Stock</th>'
            '</tr></thead><tbody>{}</tbody></table>',
            format_html("".join(str(r) for r in w_rows)) if w_rows
            else format_html('<tr><td colspan="4" style="padding:8px;color:#888;">No wholesale data</td></tr>'),
        )

        # ── Retail table ──
        r_rows = []
        for i, sp in enumerate(retail_products):
            is_cheapest = (i == 0 and sp.in_stock)
            r_rows.append(format_html(
                '<tr style="background:{};">'
                '<td style="padding:8px 10px;font-weight:{};">{}</td>'
                '<td style="padding:8px 10px;font-weight:bold;color:{};">{:.2f} MAD</td>'
                '<td style="padding:8px 10px;">{}</td>'
                '<td style="padding:8px 10px;">'
                '<a href="{}" target="_blank" style="color:#185FA5;font-size:0.85em;">View →</a>'
                '</td>'
                '</tr>',
                "#1A0A08" if not sp.in_stock else "#0A1A2A" if is_cheapest else "#0D1F2D",
                "bold" if is_cheapest else "normal",
                ("🏆 " if is_cheapest else "") + sp.site.domain,
                "#2ecc71" if is_cheapest else "#e0e0e0",
                float(sp.current_price),
                format_html('<span style="color:#2ecc71;font-size:0.85em;">✓ In Stock</span>')
                if sp.in_stock else
                format_html('<span style="color:#e74c3c;font-size:0.85em;">✗ Out of Stock</span>'),
                sp.product_url,
            ))

        retail_table = format_html(
            '<h4 style="color:#e74c3c;margin:0 0 8px;font-size:0.85em;'
            'text-transform:uppercase;letter-spacing:0.05em;">🛒 Retail Market Prices</h4>'
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
            'font-family:inherit;max-width:900px;">'
            '{margin}{wholesale}{retail}</div>',
            margin=margin_html,
            wholesale=wholesale_table,
            retail=retail_table,
        )

    # -- Actions -------------------------------------------------------------

    @admin.action(description="🔍 Check availability (use API endpoint for production)")
    def check_availability(self, request, queryset):
        messages.warning(
            request,
            "Availability checks require credentials from the ERP system. "
            "Use POST /api/v1/grossiste/check-availability/ with credentials in the payload, "
            "or use the management command: "
            "python manage.py sync_grossiste --name GPM --check-stock "
            "--codes 5230 --username user --password pass"
        )

    @admin.action(description="🛒 Create draft order (qty=1)")
    def create_draft_order(self, request, queryset):
        for product in queryset.select_related("grossiste"):
            order = GrossisteOrder.objects.create(
                grossiste=product.grossiste,
                product=product,
                quantity=1,
                unit_price=product.prix_pharmacien,
                status=GrossisteOrder.Status.DRAFT,
            )
            messages.info(
                request,
                f"Draft order #{order.pk} created for {product.name}.",
            )

    @admin.action(description="✅ Mark master product link as verified")
    def verify_match(self, request, queryset):
        updated = queryset.update(manually_verified=True)
        messages.success(request, f"{updated} product(s) marked as verified.")

    actions = ["check_availability", "create_draft_order", "verify_match"]


# ---------------------------------------------------------------------------
# GrossisteOrder Admin
# ---------------------------------------------------------------------------

@admin.register(GrossisteOrder)
class GrossisteOrderAdmin(admin.ModelAdmin):
    list_display = [
        "id", "grossiste", "product_name", "quantity",
        "unit_price", "total_col", "status_badge", "created_at",
    ]
    list_filter   = ["status", "grossiste"]
    search_fields = ["product__name", "product__code", "external_order_id"]
    readonly_fields = [
        "grossiste", "product", "unit_price", "external_order_id",
        "response_payload", "submitted_at", "created_at", "updated_at",
    ]
    ordering = ["-created_at"]
    list_select_related = ["grossiste", "product"]

    fieldsets = (
        ("Order", {"fields": ("grossiste", "product", "quantity", "unit_price", "status", "notes")}),
        ("Submission", {
            "fields": ("external_order_id", "submitted_at", "error_message", "response_payload"),
            "description": "Populated after the order is submitted to the grossiste API.",
        }),
        ("Timestamps", {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Product")
    def product_name(self, obj: GrossisteOrder) -> str:
        return f"{obj.product.code} — {obj.product.name}"

    @admin.display(description="Total (MAD)")
    def total_col(self, obj: GrossisteOrder) -> str:
        total = obj.total_price
        if total is None:
            return "—"
        return format_html("<b>{:.2f}</b>", total)

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

    @admin.action(description="📤 Submit selected DRAFT orders")
    def submit_orders(self, request, queryset):
        for order in queryset.filter(status=GrossisteOrder.Status.DRAFT).select_related("grossiste", "product"):
            try:
                result = asyncio.run(_submit_order(order))
                order.status           = GrossisteOrder.Status.SUBMITTED
                order.response_payload = result
                order.submitted_at     = timezone.now()
                order.save(update_fields=["status", "response_payload", "submitted_at"])
                messages.info(request, f"Order #{order.pk}: {result.get('message', 'submitted')}")
            except (GrossisteAuthError, GrossisteAPIError) as exc:
                order.status        = GrossisteOrder.Status.FAILED
                order.error_message = str(exc)
                order.save(update_fields=["status", "error_message"])
                messages.error(request, f"Order #{order.pk} failed: {exc}")

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status != GrossisteOrder.Status.DRAFT:
            return False
        return True

    actions = ["submit_orders"]


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

async def _sync_catalogue(config: GrossisteConfig) -> dict:
    async with GrossisteClient(config) as client:
        await client.login()
        products = await client.fetch_product_list()

    created = updated = 0
    for p in products:
        _, was_created = await GrossisteProduct.objects.aupdate_or_create(
            grossiste=config,
            code=p["code"],
            defaults={
                "name":             p["name"],
                "prix_pharmacien":  p["prix_pharmacien"],
                "ppm":              p["ppm"],
                "pa":               p["pa"],
                "forme":            p["forme"],
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1
    return {"created": created, "updated": updated}


async def _check_availability_batch(config, products) -> dict:
    results: dict[str, bool] = {}
    now = timezone.now()
    async with GrossisteClient(config) as client:
        await client.login()
        for product in products:
            in_stock = await client.check_availability(product.code)
            results[product.code] = in_stock
            await GrossisteProduct.objects.filter(pk=product.pk).aupdate(
                in_stock=in_stock,
                availability_checked_at=now,
            )
    return results


async def _submit_order(order) -> dict:
    async with GrossisteClient(order.grossiste) as client:
        await client.login()
        return await client.place_order(
            product_code=order.product.code,
            quantity=order.quantity,
            notes=order.notes,
        )