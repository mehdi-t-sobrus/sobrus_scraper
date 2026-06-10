"""
src/backend/core/wsgi.py
========================
WSGI entry point — kept for management commands and compatibility.
The production server uses ASGI (core.asgi) via Uvicorn.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

application = get_wsgi_application()
