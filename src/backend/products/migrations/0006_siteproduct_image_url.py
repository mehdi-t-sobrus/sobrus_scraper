"""
src/backend/products/migrations/0006_siteproduct_image_url.py
==============================================================
Adds image_url field to SiteProduct — stores the primary product
image URL from each site's listing.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("products", "0005_siteproduct_unique_per_master_per_site"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteproduct",
            name="image_url",
            field=models.URLField(
                blank=True,
                default="",
                max_length=2048,
                help_text="Primary product image URL from this site.",
            ),
        ),
    ]
