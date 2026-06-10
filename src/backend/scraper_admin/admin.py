"""
src/backend/scraper_admin/admin.py
===================================
Django Admin registrations for the scraper control plane.

Per CLAUDE.md §1, the Admin is the primary command dashboard for:
  - Monitoring URL discovery and scrape queue depth
  - Live-adjusting per-domain throttle settings (no redeploy required)
  - Toggling proxy endpoints in/out of rotation
  - Inspecting per-job scrape logs and R2 Bronze pointers
  - Manually suppressing URLs (status = EXCLUDED)
"""

from __future__ import annotations

from django.contrib import admin
from django.db.models import Count, QuerySet
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .models import ProxyPool, ScrapeLog, ScrapedURL, SiteConfig


# ---------------------------------------------------------------------------
# SiteConfig Admin
# ---------------------------------------------------------------------------

@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "domain",
        "status_badge",
        "max_concurrency",
        "request_delay_ms",
        "url_count",
        "updated_at",
    ]
    list_filter = ["status", "impersonate_profile"]
    search_fields = ["name", "domain", "base_url"]
    readonly_fields = ["id", "created_at", "updated_at"]
    ordering = ["name"]

    fieldsets = (
        (
            "Identity",
            {
                "fields": ("id", "name", "base_url", "domain", "status"),
            },
        ),
        (
            "Throttle Settings",
            {
                "description": (
                    "⚠️  Per CLAUDE.md §4, max_concurrency must not exceed 5 "
                    "without explicit ops approval."
                ),
                "fields": (
                    "max_concurrency",
                    "request_delay_ms",
                    "retry_backoff_base_seconds",
                    "max_retries",
                    "impersonate_profile",
                ),
            },
        ),
        (
            "URL Discovery",
            {
                "classes": ("collapse",),
                "fields": (
                    "sitemap_url",
                    "category_url_patterns",
                    "product_url_patterns",
                ),
            },
        ),
        (
            "Metadata",
            {
                "classes": ("collapse",),
                "fields": ("notes", "created_at", "updated_at"),
            },
        ),
    )

    # -- Custom columns ------------------------------------------------------

    @admin.display(description="Status")
    def status_badge(self, obj: SiteConfig) -> str:
        colours = {
            SiteConfig.Status.ACTIVE: "#2ecc71",
            SiteConfig.Status.PAUSED: "#f39c12",
            SiteConfig.Status.BLOCKED: "#e74c3c",
            SiteConfig.Status.ARCHIVED: "#95a5a6",
        }
        colour = colours.get(obj.status, "#999")
        return format_html(
            '<span style="color:{};font-weight:bold;">● {}</span>',
            colour,
            obj.get_status_display(),
        )

    @admin.display(description="URLs")
    def url_count(self, obj: SiteConfig) -> int:
        return obj.scraped_urls.count()

    # -- Bulk actions --------------------------------------------------------

    @admin.action(description="Pause selected sites")
    def pause_sites(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(status=SiteConfig.Status.PAUSED)
        self.message_user(request, f"{updated} site(s) paused.")

    @admin.action(description="Activate selected sites")
    def activate_sites(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(status=SiteConfig.Status.ACTIVE)
        self.message_user(request, f"{updated} site(s) activated.")

    actions = ["pause_sites", "activate_sites"]


# ---------------------------------------------------------------------------
# ProxyPool Admin
# ---------------------------------------------------------------------------

@admin.register(ProxyPool)
class ProxyPoolAdmin(admin.ModelAdmin):
    list_display = [
        "provider",
        "proxy_type",
        "country_code",
        "is_active",
        "consecutive_failures",
        "last_success_at",
        "last_failure_at",
    ]
    list_filter = ["is_active", "proxy_type", "provider", "country_code"]
    search_fields = ["provider", "country_code"]
    readonly_fields = [
        "id",
        "last_success_at",
        "last_failure_at",
        "consecutive_failures",
        "created_at",
        "updated_at",
    ]
    ordering = ["-is_active", "provider"]

    # Hide raw endpoint in list — credentials visible only in detail view
    fieldsets = (
        (
            "Endpoint",
            {
                "description": "Credentials are stored encrypted at rest via DB-level encryption.",
                "fields": ("id", "endpoint", "proxy_type", "provider", "country_code", "is_active"),
            },
        ),
        (
            "Health",
            {
                "fields": (
                    "consecutive_failures",
                    "last_success_at",
                    "last_failure_at",
                ),
            },
        ),
        (
            "Timestamps",
            {
                "classes": ("collapse",),
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    @admin.action(description="Deactivate selected proxies")
    def deactivate_proxies(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} proxy/proxies deactivated.")

    @admin.action(description="Activate selected proxies")
    def activate_proxies(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(is_active=True, consecutive_failures=0)
        self.message_user(request, f"{updated} proxy/proxies activated.")

    @admin.action(description="Reset failure counter")
    def reset_failure_counter(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(consecutive_failures=0)
        self.message_user(request, f"Reset failure counter on {updated} proxy/proxies.")

    actions = ["deactivate_proxies", "activate_proxies", "reset_failure_counter"]


# ---------------------------------------------------------------------------
# ScrapedURL Admin
# ---------------------------------------------------------------------------

class ScrapedURLStatusFilter(admin.SimpleListFilter):
    """Filter the queue by actionable status groups."""

    title = "Queue Status Group"
    parameter_name = "status_group"

    def lookups(self, request: HttpRequest, model_admin) -> list[tuple[str, str]]:
        return [
            ("needs_work", "🟡 Needs work (pending + failed)"),
            ("in_flight", "🔵 In flight (in_progress)"),
            ("terminal", "🔴 Terminal (blocked + not_found)"),
            ("done", "✅ Done"),
        ]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        mapping = {
            "needs_work": [ScrapedURL.Status.PENDING, ScrapedURL.Status.FAILED],
            "in_flight": [ScrapedURL.Status.IN_PROGRESS],
            "terminal": [ScrapedURL.Status.BLOCKED, ScrapedURL.Status.NOT_FOUND],
            "done": [ScrapedURL.Status.DONE],
        }
        statuses = mapping.get(self.value())
        if statuses:
            return queryset.filter(status__in=statuses)
        return queryset


@admin.register(ScrapedURL)
class ScrapedURLAdmin(admin.ModelAdmin):
    list_display = [
        "truncated_url",
        "site",
        "url_type",
        "status",
        "priority",
        "discovered_at",
        "last_scraped_at",
        "next_scrape_after",
    ]
    list_filter = [ScrapedURLStatusFilter, "url_type", "site"]
    search_fields = ["url", "arq_job_id"]
    readonly_fields = ["id", "discovered_at", "last_scraped_at", "arq_job_id"]
    ordering = ["-priority", "discovered_at"]
    list_per_page = 100

    fieldsets = (
        (
            "URL",
            {"fields": ("id", "site", "url", "url_type")},
        ),
        (
            "Queue State",
            {
                "fields": (
                    "status",
                    "priority",
                    "arq_job_id",
                    "next_scrape_after",
                ),
            },
        ),
        (
            "Timestamps",
            {
                "classes": ("collapse",),
                "fields": ("discovered_at", "last_scraped_at"),
            },
        ),
    )

    @admin.display(description="URL")
    def truncated_url(self, obj: ScrapedURL) -> str:
        return obj.url[:80] + ("…" if len(obj.url) > 80 else "")

    @admin.action(description="Mark selected URLs as Pending (re-queue)")
    def requeue_urls(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(
            status=ScrapedURL.Status.PENDING, arq_job_id="", next_scrape_after=None
        )
        self.message_user(request, f"{updated} URL(s) re-queued.")

    @admin.action(description="Exclude selected URLs from scraping")
    def exclude_urls(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(status=ScrapedURL.Status.EXCLUDED)
        self.message_user(request, f"{updated} URL(s) excluded.")

    actions = ["requeue_urls", "exclude_urls"]


# ---------------------------------------------------------------------------
# ScrapeLog Admin  (read-only — append-only per CLAUDE.md §3)
# ---------------------------------------------------------------------------

@admin.register(ScrapeLog)
class ScrapeLogAdmin(admin.ModelAdmin):
    list_display = [
        "fetched_at",
        "site",
        "status_badge",
        "http_status_code",
        "elapsed_seconds",
        "attempt_count",
        "content_length_bytes",
        "r2_link",
    ]
    list_filter = ["status", "site", "http_status_code"]
    search_fields = ["url", "arq_job_id", "error_message", "r2_bronze_key"]
    readonly_fields = [
        f.name for f in ScrapeLog._meta.get_fields()
        if hasattr(f, "name") and f.name != "id"
    ] + ["id"]
    ordering = ["-fetched_at"]
    list_per_page = 200
    date_hierarchy = "fetched_at"

    # ScrapeLog is append-only — block all mutations via Admin
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    @admin.display(description="Status")
    def status_badge(self, obj: ScrapeLog) -> str:
        colours = {
            "success": "#2ecc71",
            "blocked": "#e74c3c",
            "not_found": "#e67e22",
            "timeout": "#9b59b6",
            "rate_limited": "#f39c12",
            "http_error": "#c0392b",
            "network_error": "#c0392b",
            "unknown_error": "#7f8c8d",
        }
        colour = colours.get(obj.status, "#999")
        return format_html(
            '<span style="color:{};font-weight:bold;">● {}</span>',
            colour,
            obj.get_status_display(),
        )

    @admin.display(description="R2 Object")
    def r2_link(self, obj: ScrapeLog) -> str:
        if obj.r2_bronze_key:
            return format_html(
                '<code title="{}">{}</code>',
                obj.r2_bronze_key,
                obj.r2_bronze_key[-40:],
            )
        return "—"
