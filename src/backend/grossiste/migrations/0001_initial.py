"""
src/backend/grossiste/migrations/0001_initial.py
"""

import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("products", "0007_dailypricelog_set_null_fks"),
    ]

    operations = [
        migrations.CreateModel(
            name="GrossisteConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True)),
                ("name", models.CharField(max_length=128, unique=True)),
                ("domain", models.URLField(max_length=256, unique=True)),
                ("username", models.CharField(max_length=128)),
                ("password", models.CharField(max_length=256)),
                ("is_active", models.BooleanField(default=True)),
                ("login_path", models.CharField(default="/login", max_length=128)),
                ("products_path", models.CharField(default="/GetProd", max_length=128)),
                ("order_path", models.CharField(default="/order", max_length=128)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name": "Grossiste Config",
                     "verbose_name_plural": "Grossiste Configs",
                     "ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="GrossisteProduct",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True)),
                ("grossiste", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="products",
                    to="grossiste.grossisteconfig",
                )),
                ("master_product", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="grossiste_products",
                    to="products.masterproduct",
                )),
                ("code", models.CharField(db_index=True, max_length=32)),
                ("name", models.CharField(max_length=512)),
                ("prix_pharmacien", models.DecimalField(
                    blank=True, decimal_places=3, max_digits=12, null=True)),
                ("ppm", models.DecimalField(
                    blank=True, decimal_places=2, max_digits=12, null=True)),
                ("pa", models.DecimalField(
                    blank=True, decimal_places=3, max_digits=12, null=True)),
                ("forme", models.CharField(blank=True, default="", max_length=8)),
                ("match_confidence", models.FloatField(blank=True, null=True)),
                ("manually_verified", models.BooleanField(default=False)),
                ("in_stock", models.BooleanField(blank=True, null=True, default=None)),
                ("availability_checked_at", models.DateTimeField(blank=True, null=True)),
                ("synced_at", models.DateTimeField(auto_now=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"verbose_name": "Grossiste Product",
                     "verbose_name_plural": "Grossiste Products",
                     "ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="GrossisteOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True)),
                ("grossiste", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="orders",
                    to="grossiste.grossisteconfig",
                )),
                ("product", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="orders",
                    to="grossiste.grossisteproduct",
                )),
                ("quantity", models.PositiveIntegerField(default=1)),
                ("unit_price", models.DecimalField(
                    blank=True, decimal_places=3, max_digits=12, null=True)),
                ("status", models.CharField(
                    choices=[("draft", "Draft — not yet submitted"),
                             ("submitted", "Submitted to grossiste"),
                             ("confirmed", "Confirmed by grossiste"),
                             ("failed", "Submission failed")],
                    db_index=True, default="draft", max_length=16)),
                ("external_order_id", models.CharField(blank=True, default="", max_length=256)),
                ("response_payload", models.JSONField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True, default="")),
                ("notes", models.TextField(blank=True, default="")),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name": "Grossiste Order",
                     "verbose_name_plural": "Grossiste Orders",
                     "ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="grossisteproduct",
            constraint=models.UniqueConstraint(
                fields=["grossiste", "code"],
                name="unique_grossiste_product_code",
            ),
        ),
        migrations.AddIndex(
            model_name="grossisteproduct",
            index=models.Index(fields=["code"], name="idx_gros_code"),
        ),
        migrations.AddIndex(
            model_name="grossisteproduct",
            index=models.Index(fields=["name"], name="idx_gros_name"),
        ),
        migrations.AddIndex(
            model_name="grossisteproduct",
            index=models.Index(fields=["grossiste", "in_stock"], name="idx_gros_stock"),
        ),
        migrations.AddIndex(
            model_name="grossisteproduct",
            index=models.Index(fields=["master_product"], name="idx_gros_master"),
        ),
    ]
