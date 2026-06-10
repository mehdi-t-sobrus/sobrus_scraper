"""
src/backend/core/urls.py
========================
Root URL configuration.

Routes
------
/admin/     → Django Admin (command dashboard per CLAUDE.md §1)
/api/v1/    → Django Ninja API (scraper_admin + products endpoints)
/health/    → Lightweight liveness probe (Docker / Nginx healthcheck)
"""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import path

from core.api import api

admin.site.site_header = "Pipeline Admin"
admin.site.site_title = "Pipeline Control"
admin.site.index_title = "Medallion Pipeline Dashboard"


def health_check(request):
    """Lightweight liveness probe — returns 200 when Django is up."""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    path("health/", health_check),
]
