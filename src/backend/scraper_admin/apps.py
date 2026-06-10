"""
src/backend/scraper_admin/apps.py
"""
from django.apps import AppConfig


class ScraperAdminConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scraper_admin"
    verbose_name = "Scraper Control Plane"
