"""
src/backend/grossiste/migrations/0002_remove_credentials.py
============================================================
Removes username and password fields from GrossisteConfig.

Credentials are now passed per-request from the ERP system and
used on-the-fly — they are never stored in the database.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("grossiste", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="grossisteconfig",
            name="username",
        ),
        migrations.RemoveField(
            model_name="grossisteconfig",
            name="password",
        ),
    ]
