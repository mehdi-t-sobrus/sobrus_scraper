"""
src/backend/core/settings.py
============================
Single settings file with full environment-variable override support.

All secrets and environment-specific values are read from .env (local dev)
or the real environment (Docker / production).
Load order: .env file → environment variable → sensible default.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Resolves to src/backend/
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from src/backend/.env — no-op if the file doesn't exist (prod/Docker)
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Core Django
# ---------------------------------------------------------------------------

SECRET_KEY: str = os.environ["DJANGO_SECRET_KEY"]

DEBUG: bool = os.getenv("DJANGO_DEBUG", "False").lower() in {"1", "true", "yes"}

ALLOWED_HOSTS: list[str] = [
    h.strip()
    for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

CSRF_TRUSTED_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "http://localhost").split(",")
    if o.strip()
]

WSGI_APPLICATION = "core.wsgi.application"
ASGI_APPLICATION = "core.asgi.application"

ROOT_URLCONF = "core.urls"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SITE_ID = 1


# ---------------------------------------------------------------------------
# Installed Applications
# ---------------------------------------------------------------------------

DJANGO_APPS: list[str] = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS: list[str] = [
    "ninja",          # Django Ninja API framework
    "corsheaders",    # django-cors-headers
]

LOCAL_APPS: list[str] = [
    "scraper_admin",
    "products",
]

INSTALLED_APPS: list[str] = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE: list[str] = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",          # must be before CommonMiddleware
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Database — PostgreSQL + TimescaleDB
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "pipeline_gold"),
        "USER": os.getenv("POSTGRES_USER", "pipeline_user"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
        "OPTIONS": {
            "options": "-c search_path=public",
        },
    }
}


# ---------------------------------------------------------------------------
# Cache — Redis
# ---------------------------------------------------------------------------

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.getenv("DJANGO_CACHE_REDIS_URL", "redis://localhost:6379/1"),
        "KEY_PREFIX": "pipeline_cache",
        "TIMEOUT": 300,
    }
}


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"


# ---------------------------------------------------------------------------
# Authentication & Password Validation
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/admin/"


# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static & Media Files
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL: str = os.getenv("DJANGO_LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} [{levelname}] {name} — {message}",
            "style": "{",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        },
        "simple": {
            "format": "[{levelname}] {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": os.getenv("DJANGO_CORE_LOG_LEVEL", "WARNING"),
            "propagate": False,
        },
        "scraper_admin": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "products": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Security Headers (production hardening — safe defaults for local dev)
# ---------------------------------------------------------------------------

SECURE_SSL_REDIRECT: bool = os.getenv("DJANGO_SECURE_SSL_REDIRECT", "False").lower() in {
    "1", "true", "yes"
}
SECURE_HSTS_SECONDS: int = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS: bool = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD: bool = SECURE_HSTS_SECONDS > 0
SECURE_CONTENT_TYPE_NOSNIFF: bool = True
SECURE_BROWSER_XSS_FILTER: bool = True
X_FRAME_OPTIONS: str = "DENY"
CSRF_COOKIE_SECURE: bool = not DEBUG


# ---------------------------------------------------------------------------
# CORS (Django Ninja API — allow Dagster UI / internal services)
# ---------------------------------------------------------------------------

CORS_ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv(
        "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000"
    ).split(",")
    if o.strip()
]
CORS_ALLOW_CREDENTIALS: bool = True


# ---------------------------------------------------------------------------
# Cloudflare R2 / S3-compatible Object Storage
# ---------------------------------------------------------------------------

R2_ACCOUNT_ID: str = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID: str = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BRONZE_BUCKET: str = os.getenv("R2_BRONZE_BUCKET", "pipeline-bronze")
R2_SILVER_BUCKET: str = os.getenv("R2_SILVER_BUCKET", "pipeline-silver")
R2_ENDPOINT_URL: str = os.getenv(
    "R2_ENDPOINT_URL",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else "",
)


# ---------------------------------------------------------------------------
# API Authentication
# ---------------------------------------------------------------------------

# Static API key for machine-to-machine access (Arq workers, Dagster, curl).
# Generate a secure value with: openssl rand -hex 32
# Must match DJANGO_API_KEY in the worker's environment.
DJANGO_API_KEY: str = os.getenv("DJANGO_API_KEY", "")

if not DJANGO_API_KEY and not DEBUG:
    raise RuntimeError(
        "DJANGO_API_KEY must be set in production. "
        "Generate one with: openssl rand -hex 32"
    )


# ---------------------------------------------------------------------------
# Redis / Arq Scraping Queue
# ---------------------------------------------------------------------------

REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

SCRAPER_MAX_CONCURRENCY_PER_DOMAIN: int = int(
    os.getenv("SCRAPER_MAX_CONCURRENCY_PER_DOMAIN", "5")
)
SCRAPER_REQUEST_TIMEOUT_SECONDS: int = int(
    os.getenv("SCRAPER_REQUEST_TIMEOUT_SECONDS", "30")
)
SCRAPER_MAX_RETRIES: int = int(os.getenv("SCRAPER_MAX_RETRIES", "3"))


# ---------------------------------------------------------------------------
# Admin bulk operations
# ---------------------------------------------------------------------------

# Raised when selecting thousands of checkboxes in Admin bulk actions.
# Default is 1000 — increase for large catalogue operations.
DATA_UPLOAD_MAX_NUMBER_FIELDS: int = int(
    os.getenv("DATA_UPLOAD_MAX_NUMBER_FIELDS", "50000")
)