"""
src/backend/core/api.py
=======================
Django Ninja root API instance.

Auth strategy
-------------
Two schemes are supported and can be used together via ninja's `MultipleAuth`:

1. django_auth     — Django session cookie. Used by the browser-based Admin
                     UI and the Swagger docs UI (/api/v1/docs).

2. ApiKeyAuth      — Static API key sent in the X-API-Key header. Used by
                     machine-to-machine callers: Arq workers, Dagster sensors,
                     CLI tools, and curl.

The key is read from the DJANGO_API_KEY environment variable.  In dev this
can be any string you set in .env.  In production it should be a securely
generated random value (e.g. `openssl rand -hex 32`).

Public endpoints (e.g. /health/) bypass auth entirely at the view level.
"""

from __future__ import annotations

import os

from ninja import NinjaAPI
from ninja.security import HttpBearer

from scraper_admin.api import router as scraper_router
from products.api import router as products_router
from grossiste.api import router as grossiste_router


# ---------------------------------------------------------------------------
# API key auth — for Arq workers, Dagster, CLI, curl
# ---------------------------------------------------------------------------

class ApiKeyAuth(HttpBearer):
    """
    Validates the Bearer token in the Authorization header against
    DJANGO_API_KEY.  Falls back gracefully if the env var is not set
    (warns on startup — see WorkerSettings.on_startup).

    Usage:
        curl -H "Authorization: Bearer your-api-key" http://localhost:8000/api/v1/scrapers/sites/
    """

    def authenticate(self, request, token: str):
        expected = os.getenv("DJANGO_API_KEY", "")
        if not expected:
            # No key configured — reject all Bearer token requests
            return None
        if token == expected:
            return token
        return None


api_key_auth = ApiKeyAuth()


# ---------------------------------------------------------------------------
# Combined auth — accepts either a valid Django session OR a valid API key
# ---------------------------------------------------------------------------


def any_auth(request):
    """
    Accepts requests authenticated by either:
    - A valid Django session cookie (browser / Admin UI)
    - A valid Bearer API key (Arq workers, Dagster, curl)
    """
    # Check session auth first
    if request.user and request.user.is_authenticated:
        return request.user
    # Check Bearer token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        expected = os.getenv("DJANGO_API_KEY", "")
        if expected and token == expected:
            return token
    return None


# ---------------------------------------------------------------------------
# NinjaAPI instance
# ---------------------------------------------------------------------------

api = NinjaAPI(
    title="Pipeline API",
    version="1.0.0",
    description=(
        "Internal API for the Medallion scraping pipeline. "
        "Accepts Django session auth (browser) or Bearer API key (machines).\n\n"
        "Set Authorization: Bearer <DJANGO_API_KEY> for curl/worker access."
    ),
    auth=any_auth,
    docs_url="/docs",   # Swagger UI at /api/v1/docs
)

api.add_router("/scrapers/", scraper_router, tags=["Scraper Admin"])
api.add_router("/products/", products_router, tags=["Products"])
api.add_router("/grossiste/", grossiste_router, tags=["Grossiste"])


# ---------------------------------------------------------------------------
# Health check — public, no auth required (used by Docker healthchecks)
# ---------------------------------------------------------------------------

@api.get("/health/", auth=None, tags=["System"])
def health_check(request):
    """
    Public health check endpoint.
    Returns 200 if Django is running and the DB is reachable.
    Used by Docker healthchecks and load balancers.
    """
    from django.db import connection
    try:
        connection.ensure_connection()
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "version": "1.0.0",
    }
