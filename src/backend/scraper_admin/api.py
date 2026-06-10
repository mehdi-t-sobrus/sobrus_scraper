"""
src/backend/scraper_admin/api.py
=================================
Django Ninja router for the scraper control plane.

Endpoints are consumed by:
  - Arq workers  (read SiteConfig throttle + active proxies)
  - Dagster sensors  (poll pending URL queue, update job state)
  - CLI tooling  (manual re-queue, status inspection)
"""

from __future__ import annotations

import socket
from datetime import datetime
from typing import Any
from uuid import UUID

from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Router, Schema
from ninja.pagination import paginate, PageNumberPagination

from .models import ProxyPool, ScrapeLog, ScrapedURL, SiteConfig

router = Router()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SiteConfigOut(Schema):
    id: UUID
    name: str
    domain: str
    base_url: str
    status: str
    max_concurrency: int
    request_delay_ms: int
    retry_backoff_base_seconds: float
    max_retries: int
    impersonate_profile: str


class ProxyOut(Schema):
    id: UUID
    endpoint: str          # workers need the full DSN; keep this endpoint internal
    proxy_type: str
    country_code: str
    provider: str


class ScrapedURLOut(Schema):
    id: UUID
    site_id: UUID
    url: str
    url_type: str
    status: str
    priority: int
    arq_job_id: str
    discovered_at: datetime
    last_scraped_at: datetime | None
    next_scrape_after: datetime | None


class ScrapedURLStatusUpdate(Schema):
    status: str
    arq_job_id: str = ""
    last_scraped_at: datetime | None = None
    next_scrape_after: datetime | None = None


class ScrapeLogIn(Schema):
    """Payload posted by Arq worker on job completion."""
    scraped_url_id: UUID | None = None
    site_id: UUID | None = None
    url: str
    final_url: str = ""
    status: str
    http_status_code: int | None = None
    elapsed_seconds: float
    attempt_count: int = 1
    content_length_bytes: int | None = None
    content_type: str = ""
    proxy_used: str = ""
    arq_job_id: str = ""
    r2_bronze_key: str = ""
    error_message: str = ""
    fetched_at: datetime


class ScrapeLogOut(Schema):
    id: int
    uuid: str
    url: str
    status: str
    http_status_code: int | None
    elapsed_seconds: float
    attempt_count: int
    r2_bronze_key: str
    fetched_at: datetime


class PendingQueueOut(Schema):
    count: int
    urls: list[ScrapedURLOut]


# ---------------------------------------------------------------------------
# SiteConfig endpoints  (read-only — config is managed via Admin)
# ---------------------------------------------------------------------------

@router.get("/sites/", response=list[SiteConfigOut], summary="List active site configs")
async def list_sites(request, status: str = "active") -> list[dict[str, Any]]:
    """
    Return all SiteConfig records matching the given status.
    Workers call this to resolve per-domain throttle settings.
    """
    qs = SiteConfig.objects.filter(status=status).order_by("name")
    return [SiteConfigOut.from_orm(s).dict() async for s in qs]


@router.get("/sites/{site_id}/", response=SiteConfigOut, summary="Get a single site config")
async def get_site(request, site_id: UUID) -> SiteConfig:
    return await aget_object_or_404(SiteConfig, id=site_id)


# ---------------------------------------------------------------------------
# ProxyPool endpoints
# ---------------------------------------------------------------------------

@router.get("/proxies/", response=list[ProxyOut], summary="List active proxies")
async def list_active_proxies(request) -> list[dict[str, Any]]:
    """
    Return all active proxy endpoints.
    Arq workers call this to hydrate their in-process proxy pool.
    """
    qs = ProxyPool.objects.filter(is_active=True).order_by("?")  # random order
    return [ProxyOut.from_orm(p).dict() async for p in qs]


# ---------------------------------------------------------------------------
# ScrapedURL / Job Queue endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/queue/pending/",
    response=PendingQueueOut,
    summary="Pull pending product URLs for enqueueing",
)
async def get_pending_urls(
    request,
    site_id: UUID | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """
    Dagster sensors call this to fetch the next batch of pending URLs
    to enqueue as Arq jobs.

    Filters:
      - status = PENDING
      - url_type = PRODUCT
      - next_scrape_after is NULL or <= now
    Ordered by priority DESC, discovered_at ASC.
    """
    qs = ScrapedURL.objects.filter(
        status=ScrapedURL.Status.PENDING,
        url_type=ScrapedURL.URLType.PRODUCT,
    ).filter(
        models.Q(next_scrape_after__isnull=True)
        | models.Q(next_scrape_after__lte=timezone.now())
    ).select_related("site").order_by("-priority", "discovered_at")

    if site_id:
        qs = qs.filter(site_id=site_id)

    qs = qs[:limit]
    urls = [ScrapedURLOut.from_orm(u).dict() async for u in qs]
    return {"count": len(urls), "urls": urls}


@router.patch(
    "/queue/{url_id}/status/",
    response=ScrapedURLOut,
    summary="Update job state on a queued URL",
)
async def update_url_status(
    request,
    url_id: UUID,
    payload: ScrapedURLStatusUpdate,
) -> ScrapedURL:
    """
    Called by Dagster (on enqueue) and Arq workers (on completion) to
    transition a ScrapedURL through its status lifecycle.
    """
    obj = await aget_object_or_404(ScrapedURL, id=url_id)
    update_fields: list[str] = []

    if payload.status:
        obj.status = payload.status
        update_fields.append("status")
    if payload.arq_job_id is not None:
        obj.arq_job_id = payload.arq_job_id
        update_fields.append("arq_job_id")
    if payload.last_scraped_at is not None:
        obj.last_scraped_at = payload.last_scraped_at
        update_fields.append("last_scraped_at")
    if payload.next_scrape_after is not None:
        obj.next_scrape_after = payload.next_scrape_after
        update_fields.append("next_scrape_after")

    await obj.asave(update_fields=update_fields)
    return obj


# ---------------------------------------------------------------------------
# ScrapeLog endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/logs/",
    response={201: ScrapeLogOut},
    summary="Record a completed scrape job result",
)
async def create_scrape_log(request, payload: ScrapeLogIn) -> tuple[int, ScrapeLog]:
    """
    Posted by Arq workers immediately after a fetch attempt completes.
    Appends an immutable ScrapeLog row (never updated — CLAUDE.md §3).
    """
    log = await ScrapeLog.objects.acreate(
        scraped_url_id=payload.scraped_url_id,
        site_id=payload.site_id,
        url=payload.url,
        final_url=payload.final_url,
        status=payload.status,
        http_status_code=payload.http_status_code,
        elapsed_seconds=payload.elapsed_seconds,
        attempt_count=payload.attempt_count,
        content_length_bytes=payload.content_length_bytes,
        content_type=payload.content_type,
        proxy_used=payload.proxy_used,
        arq_job_id=payload.arq_job_id,
        worker_hostname=socket.gethostname(),
        r2_bronze_key=payload.r2_bronze_key,
        error_message=payload.error_message,
        fetched_at=payload.fetched_at,
    )
    return 201, log


@router.get(
    "/logs/",
    response=list[ScrapeLogOut],
    summary="List recent scrape logs",
)
@paginate(PageNumberPagination, page_size=100)
async def list_scrape_logs(
    request,
    site_id: UUID | None = None,
    status: str | None = None,
) -> Any:
    qs = ScrapeLog.objects.select_related("site").order_by("-fetched_at")
    if site_id:
        qs = qs.filter(site_id=site_id)
    if status:
        qs = qs.filter(status=status)
    return qs


# ---------------------------------------------------------------------------
# Import fix — models.Q needed in get_pending_urls
# ---------------------------------------------------------------------------
from django.db import models  # noqa: E402  (placed after class defs for readability)
