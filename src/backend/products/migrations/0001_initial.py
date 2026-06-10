"""
src/backend/products/migrations/0001_initial.py
"""
from __future__ import annotations

import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("scraper_admin", "0002_scrapelog_hypertable"),
    ]

    operations = [
        migrations.CreateModel(
            name="MasterProduct",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(db_index=True, max_length=512)),
                ("brand", models.CharField(blank=True, db_index=True, default="", max_length=128)),
                ("ean", models.CharField(blank=True, db_index=True, default="", max_length=14)),
                ("mpn", models.CharField(blank=True, default="", max_length=128)),
                ("category", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("subcategory", models.CharField(blank=True, default="", max_length=255)),
                ("tags", models.JSONField(blank=True, default=list)),
                ("description", models.TextField(blank=True, default="")),
                ("ingredients", models.TextField(blank=True, default="")),
                ("image_urls", models.JSONField(blank=True, default=list)),
                ("status", models.CharField(choices=[("active", "Active"), ("discontinued", "Discontinued"), ("under_review", "Under Review — matching needs human check")], db_index=True, default="active", max_length=16)),
                ("match_confidence", models.FloatField(default=1.0)),
                ("manually_verified", models.BooleanField(default=False)),
                ("first_seen_at", models.DateTimeField(auto_now_add=True)),
                ("last_matched_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name": "Master Product", "verbose_name_plural": "Master Products", "ordering": ["brand", "name"]},
        ),
        migrations.AddIndex(model_name="masterproduct", index=models.Index(fields=["ean"], name="idx_product_ean")),
        migrations.AddIndex(model_name="masterproduct", index=models.Index(fields=["brand", "name"], name="idx_product_brand_name")),
        migrations.AddIndex(model_name="masterproduct", index=models.Index(fields=["status", "match_confidence"], name="idx_product_review_queue")),
        migrations.CreateModel(
            name="SiteProduct",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("master_product", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="site_products", to="products.masterproduct")),
                ("site", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="site_products", to="scraper_admin.siteconfig")),
                ("scraped_url", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="site_product", to="scraper_admin.scrapedurl")),
                ("raw_name", models.CharField(max_length=512)),
                ("raw_brand", models.CharField(blank=True, default="", max_length=128)),
                ("raw_ean", models.CharField(blank=True, default="", max_length=14)),
                ("raw_category", models.CharField(blank=True, default="", max_length=255)),
                ("raw_description", models.TextField(blank=True, default="")),
                ("current_price", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("currency", models.CharField(default="EUR", max_length=3)),
                ("in_stock", models.BooleanField(db_index=True, default=True)),
                ("product_url", models.URLField(max_length=2048, unique=True)),
                ("match_score", models.FloatField(default=0.0)),
                ("first_scraped_at", models.DateTimeField(auto_now_add=True)),
                ("last_scraped_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name": "Site Product", "verbose_name_plural": "Site Products", "ordering": ["site", "raw_name"]},
        ),
        migrations.AddIndex(model_name="siteproduct", index=models.Index(fields=["site", "in_stock"], name="idx_site_product_stock")),
        migrations.AddIndex(model_name="siteproduct", index=models.Index(fields=["master_product", "site"], name="idx_site_product_master")),
        migrations.CreateModel(
            name="DailyPriceLog",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("site_product", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="price_logs", to="products.siteproduct")),
                ("master_product", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="price_logs", to="products.masterproduct")),
                ("site", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="price_logs", to="scraper_admin.siteconfig")),
                ("price", models.DecimalField(decimal_places=2, max_digits=10)),
                ("currency", models.CharField(default="EUR", max_length=3)),
                ("in_stock", models.BooleanField(default=True)),
                ("scrape_log_id", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("logged_at", models.DateTimeField(db_index=True)),
            ],
            options={"verbose_name": "Daily Price Log", "verbose_name_plural": "Daily Price Logs", "ordering": ["-logged_at"]},
        ),
        migrations.AddIndex(model_name="dailypricelog", index=models.Index(fields=["master_product", "-logged_at"], name="idx_pricelog_master_time")),
        migrations.AddIndex(model_name="dailypricelog", index=models.Index(fields=["site", "-logged_at"], name="idx_pricelog_site_time")),
    ]
