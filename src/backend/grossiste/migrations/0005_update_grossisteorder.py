"""
src/backend/grossiste/migrations/0005_update_grossisteorder.py
===============================================================
Updates GrossisteOrder to store richer Sobrus order data:
  - sale_price (retail price at time of order)
  - sobrus_order_id (e.g. "9757341")
  - sobrus_transaction_num (e.g. "BC-3579")
  - sobrus_status (e.g. "approved")
  - Makes product FK nullable
  - Removes external_order_id (replaced by sobrus_order_id)

Also adds sobrus_supplier_id to GrossisteConfig and
sobrus_product_id to GrossisteProduct (was in 0003_add_sobrus_ids
which never ran).
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("grossiste", "0004_merge_20260616_1547"),
    ]

    operations = [
        # --- Fields that were in 0003_add_sobrus_ids (never ran) ---
        migrations.AddField(
            model_name="grossisteconfig",
            name="sobrus_supplier_id",
            field=models.IntegerField(
                null=True, blank=True,
                help_text="Sobrus internal supplier ID. GPM=1, Sophasais=1570, Lodimed=346",
            ),
        ),
        migrations.AddField(
            model_name="grossisteproduct",
            name="sobrus_product_id",
            field=models.IntegerField(
                null=True, blank=True, db_index=True,
                help_text="Sobrus internal product ID used in api.pharma.sobrus.com calls.",
            ),
        ),

        # --- GrossisteOrder updates ---
        migrations.AlterField(
            model_name="grossisteorder",
            name="product",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="orders",
                to="grossiste.grossisteproduct",
            ),
        ),
        migrations.AddField(
            model_name="grossisteorder",
            name="sale_price",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=12, null=True,
                help_text="Retail sale price at time of order",
            ),
        ),
        migrations.AddField(
            model_name="grossisteorder",
            name="sobrus_order_id",
            field=models.CharField(
                blank=True, default="", max_length=64,
                help_text="Sobrus internal order ID (e.g. 9757341)",
            ),
        ),
        migrations.AddField(
            model_name="grossisteorder",
            name="sobrus_transaction_num",
            field=models.CharField(
                blank=True, default="", max_length=64,
                help_text="Sobrus transaction number (e.g. BC-3579)",
            ),
        ),
        migrations.AddField(
            model_name="grossisteorder",
            name="sobrus_status",
            field=models.CharField(
                blank=True, default="", max_length=64,
                help_text="Sobrus order status (e.g. approved)",
            ),
        ),
        migrations.RemoveField(
            model_name="grossisteorder",
            name="external_order_id",
        ),
    ]