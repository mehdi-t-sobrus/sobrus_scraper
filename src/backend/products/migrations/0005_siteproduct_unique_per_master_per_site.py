"""
src/backend/products/migrations/0005_siteproduct_unique_per_master_per_site.py
================================================================================
Adds a unique constraint ensuring one SiteProduct per (master_product, site).
This prevents the same site listing from being linked to a MasterProduct
multiple times via different product URLs — each site gets exactly one
canonical listing per master product.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("products", "0004_alter_dailypricelog_logged_at_and_more"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="siteproduct",
            constraint=models.UniqueConstraint(
                fields=["master_product", "site"],
                name="unique_siteproduct_per_master_per_site",
            ),
        ),
    ]
