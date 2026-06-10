"""
src/backend/core/asgi.py
========================
ASGI entry point — exposes the ``application`` callable for Uvicorn / Daphne.

Run via Gunicorn + UvicornWorker (see docker-compose.yml):
    gunicorn core.asgi:application --worker-class uvicorn.workers.UvicornWorker
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

application = get_asgi_application()
