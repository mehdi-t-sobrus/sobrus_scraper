"""
src/backend/products/migrations/0007_dailypricelog_set_null_fks.py
====================================================================
Changes DailyPriceLog foreign keys from CASCADE to SET_NULL.

Industry standard rationale:
    Price observations are historical facts — they don't cease to exist
    because we reorganise the product catalogue. When a MasterProduct or
    SiteProduct is deleted (e.g. orphan cleanup), we nullify the FK rather
    than destroying the price history.

    This also unblocks Admin bulk-delete of orphaned MasterProducts, which
    previously failed because Django requires delete permission on all
    cascade targets — and DailyPriceLog is a protected TimescaleDB hypertable.

After this migration:
    - Deleting a MasterProduct sets price_logs.master_product_id = NULL
    - Deleting a SiteProduct sets price_logs.site_product_id = NULL
    - Price history rows are preserved for analytics
    - Orphaned logs (NULL master/site) can be pruned later via TimescaleDB
      data retention policies if needed
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("products", "0006_siteproduct_image_url"),
        ("scraper_admin", "0002_scrapelog_hypertable"),
    ]

    operations = [
        # site_product FK: CASCADE → SET_NULL
        migrations.AlterField(
            model_name="dailypricelog",
            name="site_product",
            field=models.ForeignKey(
                blank=True,
                db_index=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="price_logs",
                to="products.siteproduct",
            ),
        ),
        # master_product FK: CASCADE → SET_NULL
        migrations.AlterField(
            model_name="dailypricelog",
            name="master_product",
            field=models.ForeignKey(
                blank=True,
                db_index=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="price_logs",
                to="products.masterproduct",
            ),
        ),
        # site FK: CASCADE → SET_NULL
        migrations.AlterField(
            model_name="dailypricelog",
            name="site",
            field=models.ForeignKey(
                blank=True,
                db_index=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="price_logs",
                to="scraper_admin.siteconfig",
            ),
        ),
    ]
