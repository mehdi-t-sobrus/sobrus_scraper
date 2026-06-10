"""
src/backend/scraper_admin/migrations/0001_initial.py
Generated initial migration for SiteConfig, ProxyPool, ScrapedURL, ScrapeLog.
"""
from __future__ import annotations

import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies: list = []

    operations = [
        migrations.CreateModel(
            name="SiteConfig",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=128, unique=True)),
                ("base_url", models.URLField(max_length=512, unique=True)),
                ("domain", models.CharField(db_index=True, max_length=253, unique=True)),
                ("status", models.CharField(choices=[("active", "Active"), ("paused", "Paused"), ("blocked", "Blocked — under investigation"), ("archived", "Archived")], db_index=True, default="active", max_length=16)),
                ("max_concurrency", models.PositiveSmallIntegerField(default=5)),
                ("request_delay_ms", models.PositiveIntegerField(default=1000)),
                ("retry_backoff_base_seconds", models.FloatField(default=5.0)),
                ("max_retries", models.PositiveSmallIntegerField(default=3)),
                ("sitemap_url", models.URLField(blank=True, default="", max_length=512)),
                ("category_url_patterns", models.JSONField(blank=True, default=list)),
                ("product_url_patterns", models.JSONField(blank=True, default=list)),
                ("impersonate_profile", models.CharField(default="chrome", max_length=32)),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name": "Site Configuration", "verbose_name_plural": "Site Configurations", "ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="ProxyPool",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("endpoint", models.CharField(max_length=512, unique=True)),
                ("proxy_type", models.CharField(choices=[("residential", "Residential"), ("datacenter", "Datacenter"), ("mobile", "Mobile"), ("isp", "ISP")], default="residential", max_length=16)),
                ("provider", models.CharField(blank=True, default="", max_length=64)),
                ("country_code", models.CharField(blank=True, default="", max_length=2)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("last_success_at", models.DateTimeField(blank=True, null=True)),
                ("last_failure_at", models.DateTimeField(blank=True, null=True)),
                ("consecutive_failures", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name": "Proxy", "verbose_name_plural": "Proxy Pool", "ordering": ["provider", "country_code"]},
        ),
        migrations.CreateModel(
            name="ScrapedURL",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("site", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="scraped_urls", to="scraper_admin.siteconfig")),
                ("url", models.URLField(db_index=True, max_length=2048)),
                ("url_type", models.CharField(choices=[("product", "Product Detail Page"), ("category", "Category / Listing Page"), ("sitemap", "Sitemap")], db_index=True, default="product", max_length=16)),
                ("status", models.CharField(choices=[("pending", "Pending — awaiting scrape"), ("in_progress", "In Progress — job enqueued"), ("done", "Done — successfully scraped"), ("blocked", "Blocked — 403 received"), ("not_found", "Not Found — 404/410"), ("failed", "Failed — exhausted retries"), ("excluded", "Excluded — manually suppressed")], db_index=True, default="pending", max_length=16)),
                ("priority", models.SmallIntegerField(db_index=True, default=0)),
                ("arq_job_id", models.CharField(blank=True, default="", max_length=128)),
                ("discovered_at", models.DateTimeField(auto_now_add=True)),
                ("last_scraped_at", models.DateTimeField(blank=True, null=True)),
                ("next_scrape_after", models.DateTimeField(blank=True, db_index=True, null=True)),
            ],
            options={"verbose_name": "Scraped URL", "verbose_name_plural": "Scraped URLs", "ordering": ["-priority", "discovered_at"]},
        ),
        migrations.AddConstraint(
            model_name="scrapedurl",
            constraint=models.UniqueConstraint(fields=["site", "url"], name="unique_site_url"),
        ),
        migrations.AddIndex(
            model_name="scrapedurl",
            index=models.Index(fields=["status", "url_type", "-priority", "next_scrape_after"], name="idx_scraped_url_queue"),
        ),
        migrations.CreateModel(
            name="ScrapeLog",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)),
                ("scraped_url", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="scrape_logs", to="scraper_admin.scrapedurl")),
                ("site", models.ForeignKey(db_index=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="scrape_logs", to="scraper_admin.siteconfig")),
                ("url", models.URLField(max_length=2048)),
                ("final_url", models.URLField(blank=True, default="", max_length=2048)),
                ("status", models.CharField(choices=[("success", "Success"), ("blocked", "Blocked (403)"), ("not_found", "Not Found (404/410)"), ("timeout", "Timeout"), ("rate_limited", "Rate Limited (429)"), ("http_error", "HTTP Error"), ("network_error", "Network Error"), ("unknown_error", "Unknown Error")], db_index=True, max_length=16)),
                ("http_status_code", models.SmallIntegerField(blank=True, null=True)),
                ("elapsed_seconds", models.FloatField()),
                ("attempt_count", models.PositiveSmallIntegerField(default=1)),
                ("content_length_bytes", models.IntegerField(blank=True, null=True)),
                ("content_type", models.CharField(blank=True, default="", max_length=128)),
                ("proxy_used", models.CharField(blank=True, default="", max_length=256)),
                ("arq_job_id", models.CharField(blank=True, default="", max_length=128)),
                ("worker_hostname", models.CharField(blank=True, default="", max_length=128)),
                ("r2_bronze_key", models.CharField(blank=True, default="", max_length=512)),
                ("error_message", models.TextField(blank=True, default="")),
                ("fetched_at", models.DateTimeField(db_index=True)),
            ],
            options={"verbose_name": "Scrape Log", "verbose_name_plural": "Scrape Logs", "ordering": ["-fetched_at"]},
        ),
        migrations.AddIndex(
            model_name="scrapelog",
            index=models.Index(fields=["site", "-fetched_at"], name="idx_scrapelog_site_time"),
        ),
        migrations.AddIndex(
            model_name="scrapelog",
            index=models.Index(fields=["status", "-fetched_at"], name="idx_scrapelog_status_time"),
        ),
    ]
