"""
src/backend/products/api.py
============================
Django Ninja router for the Gold product catalog.

Endpoints are consumed by:
  - The matching pipeline (entity_res.py) to upsert MasterProducts / SiteProducts
  - Dagster Gold assets to bulk-write DailyPriceLogs
  - External API consumers (dashboards, reporting tools)
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Router, Schema
from ninja.pagination import paginate, PageNumberPagination

from .models import DailyPriceLog, MasterProduct, SiteProduct

router = Router()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class MasterProductOut(Schema):
    id: UUID
    name: str
    brand: str
    ean: str
    mpn: str
    category: str
    status: str
    match_confidence: float
    manually_verified: bool
    first_seen_at: datetime
    last_matched_at: datetime | None


class MasterProductIn(Schema):
    """Used by entity_res.py to upsert canonical products."""
    name: str
    brand: str = ""
    ean: str = ""
    mpn: str = ""
    category: str = ""
    subcategory: str = ""
    description: str = ""
    ingredients: str = ""
    image_urls: list[str] = []
    tags: list[str] = []
    match_confidence: float = 1.0


class SiteProductIn(Schema):
    """Posted by entity_res.py after matching a raw listing to a master product."""
    master_product_id: UUID
    site_id: UUID
    scraped_url_id: UUID | None = None
    raw_name: str
    raw_brand: str = ""
    raw_ean: str = ""
    raw_category: str = ""
    raw_description: str = ""
    current_price: Decimal | None = None
    currency: str = "EUR"
    in_stock: bool = True
    product_url: str
    match_score: float = 0.0
    last_scraped_at: datetime | None = None


class SiteProductOut(Schema):
    id: UUID
    master_product_id: UUID
    site_id: UUID
    raw_name: str
    current_price: Decimal | None
    currency: str
    in_stock: bool
    product_url: str
    match_score: float
    last_scraped_at: datetime | None


class DailyPriceLogIn(Schema):
    """Bulk price log payload from the Gold Dagster asset."""
    site_product_id: UUID
    master_product_id: UUID
    site_id: UUID
    price: Decimal
    currency: str = "EUR"
    in_stock: bool = True
    scrape_log_id: int | None = None
    logged_at: datetime


class PriceHistoryOut(Schema):
    logged_at: datetime
    price: Decimal
    currency: str
    in_stock: bool
    site_id: UUID


# ---------------------------------------------------------------------------
# MasterProduct endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/master/",
    response=list[MasterProductOut],
    summary="List master products",
)
@paginate(PageNumberPagination, page_size=100)
async def list_master_products(
    request,
    brand: str | None = None,
    status: str = "active",
    needs_review: bool = False,
) -> Any:
    qs = MasterProduct.objects.filter(status=status).order_by("brand", "name")
    if brand:
        qs = qs.filter(brand__iexact=brand)
    if needs_review:
        qs = qs.filter(match_confidence__lt=0.85, manually_verified=False)
    return qs


@router.get(
    "/master/{product_id}/",
    response=MasterProductOut,
    summary="Get a single master product",
)
async def get_master_product(request, product_id: UUID) -> MasterProduct:
    return await aget_object_or_404(MasterProduct, id=product_id)


@router.post(
    "/master/",
    response={201: MasterProductOut},
    summary="Create or update a master product (upsert by EAN or name+brand)",
)
async def upsert_master_product(
    request, payload: MasterProductIn
) -> tuple[int, MasterProduct]:
    """
    Called by ``entity_res.py`` after each matching run.
    Upserts by EAN if available, otherwise by (name, brand) pair.
    """
    lookup: dict[str, Any] = {}
    if payload.ean:
        lookup = {"ean": payload.ean}
    else:
        lookup = {"name__iexact": payload.name, "brand__iexact": payload.brand}

    defaults = payload.dict(exclude={"ean"} if payload.ean else set())
    defaults["last_matched_at"] = timezone.now()

    obj, _ = await MasterProduct.objects.aupdate_or_create(
        **lookup,
        defaults=defaults,
    )
    return 201, obj


# ---------------------------------------------------------------------------
# SiteProduct endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/site-products/",
    response={201: SiteProductOut},
    summary="Upsert a site product listing",
)
async def upsert_site_product(
    request, payload: SiteProductIn
) -> tuple[int, SiteProduct]:
    """Upserts by ``product_url`` (unique per site listing)."""
    obj, _ = await SiteProduct.objects.aupdate_or_create(
        product_url=payload.product_url,
        defaults=payload.dict(exclude={"product_url"}),
    )
    return 201, obj


@router.get(
    "/master/{product_id}/site-products/",
    response=list[SiteProductOut],
    summary="List all site products for a master product",
)
async def list_site_products(request, product_id: UUID) -> list[dict[str, Any]]:
    qs = SiteProduct.objects.filter(master_product_id=product_id).select_related("site")
    return [SiteProductOut.from_orm(sp).dict() async for sp in qs]


# ---------------------------------------------------------------------------
# DailyPriceLog endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/price-logs/bulk/",
    response={201: dict},
    summary="Bulk-insert daily price logs (Gold Dagster asset)",
)
async def bulk_create_price_logs(
    request,
    payload: list[DailyPriceLogIn],
) -> tuple[int, dict[str, Any]]:
    """
    Bulk-inserts DailyPriceLog rows.  Ignores conflicts (idempotent on
    logged_at + site_product_id) so the Dagster asset can safely retry.
    """
    logs = [
        DailyPriceLog(
            site_product_id=item.site_product_id,
            master_product_id=item.master_product_id,
            site_id=item.site_id,
            price=item.price,
            currency=item.currency,
            in_stock=item.in_stock,
            scrape_log_id=item.scrape_log_id,
            logged_at=item.logged_at,
        )
        for item in payload
    ]
    created = await DailyPriceLog.objects.abulk_create(
        logs,
        ignore_conflicts=True,
    )
    return 201, {"inserted": len(created)}


@router.get(
    "/master/{product_id}/price-history/",
    response=list[PriceHistoryOut],
    summary="Price history for a master product across all sites",
)
@paginate(PageNumberPagination, page_size=200)
async def get_price_history(
    request,
    product_id: UUID,
    site_id: UUID | None = None,
) -> Any:
    """
    Returns time-ordered price observations for a MasterProduct.
    Filter by site_id to get a single-site price trend.
    """
    qs = (
        DailyPriceLog.objects.filter(master_product_id=product_id)
        .select_related("site")
        .order_by("-logged_at")
    )
    if site_id:
        qs = qs.filter(site_id=site_id)
    return qs
