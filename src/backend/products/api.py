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
    """Single price log entry for time-series history."""
    logged_at:  datetime
    price:      Decimal
    currency:   str
    in_stock:   bool
    site_id:    UUID


class SitePriceOut(Schema):
    """Current price for a product on a specific site."""
    site_id:    UUID
    site_name:  str
    site_domain: str
    price:      Decimal | None
    currency:   str
    in_stock:   bool
    product_url: str
    last_scraped_at: datetime | None


class PriceComparisonOut(Schema):
    """
    Price comparison summary for a MasterProduct across all sites.
    The core output of the pipeline — answers "where is this cheapest?"
    """
    master_product_id:   UUID
    name:                str
    brand:               str
    ean:                 str
    category:            str
    # Aggregate pricing across all sites
    price_min:           Decimal | None   # cheapest current price
    price_max:           Decimal | None   # most expensive current price
    price_avg:           Decimal | None   # average across sites
    price_currency:      str
    sites_count:         int              # number of sites selling this
    in_stock_count:      int              # number of sites with stock
    cheapest_site:       str | None       # domain of cheapest site
    cheapest_url:        str | None       # direct link to cheapest listing
    # Per-site breakdown
    sites:               list[SitePriceOut]


# ---------------------------------------------------------------------------
# Price comparison endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/master/{product_id}/price-comparison/",
    response=PriceComparisonOut,
    summary="Price comparison for a product across all sites",
)
async def get_price_comparison(request, product_id: UUID) -> dict[str, Any]:
    """
    Returns current prices for a MasterProduct across all sites,
    with min/max/avg aggregates and the cheapest site identified.

    This is the core endpoint for the price comparison use case.
    """
    master = await aget_object_or_404(MasterProduct, id=product_id)

    site_products = [
        sp async for sp in
        SiteProduct.objects.filter(master_product_id=product_id)
        .select_related("site")
        .order_by("current_price")
    ]

    if not site_products:
        return {
            "master_product_id": product_id,
            "name": master.name,
            "brand": master.brand,
            "ean": master.ean,
            "category": master.category,
            "price_min": None,
            "price_max": None,
            "price_avg": None,
            "price_currency": "MAD",
            "sites_count": 0,
            "in_stock_count": 0,
            "cheapest_site": None,
            "cheapest_url": None,
            "sites": [],
        }

    # Build per-site list (already ordered by price ASC)
    sites_data = [
        SitePriceOut(
            site_id=sp.site_id,
            site_name=sp.site.name,
            site_domain=sp.site.domain,
            price=sp.current_price,
            currency=sp.currency,
            in_stock=sp.in_stock,
            product_url=sp.product_url,
            last_scraped_at=sp.last_scraped_at,
        )
        for sp in site_products
    ]

    # Compute aggregates from in-stock sites only (out-of-stock prices are stale)
    priced = [sp for sp in site_products if sp.current_price is not None]
    in_stock_priced = [sp for sp in priced if sp.in_stock]
    comparison_pool = in_stock_priced if in_stock_priced else priced

    price_min = min((sp.current_price for sp in comparison_pool), default=None)
    price_max = max((sp.current_price for sp in comparison_pool), default=None)
    price_avg = (
        sum(sp.current_price for sp in comparison_pool) / len(comparison_pool)
        if comparison_pool else None
    )

    cheapest = comparison_pool[0] if comparison_pool else None

    return {
        "master_product_id": product_id,
        "name": master.name,
        "brand": master.brand,
        "ean": master.ean,
        "category": master.category,
        "price_min": price_min,
        "price_max": price_max,
        "price_avg": round(price_avg, 2) if price_avg else None,
        "price_currency": priced[0].currency if priced else "MAD",
        "sites_count": len(site_products),
        "in_stock_count": sum(1 for sp in site_products if sp.in_stock),
        "cheapest_site": cheapest.site.domain if cheapest else None,
        "cheapest_url": cheapest.product_url if cheapest else None,
        "sites": sites_data,
    }


@router.get(
    "/price-comparison/",
    response=list[PriceComparisonOut],
    summary="Price comparison for all products — filterable by brand, category, name",
)
@paginate(PageNumberPagination, page_size=50)
async def list_price_comparisons(
    request,
    brand: str | None = None,
    category: str | None = None,
    name: str | None = None,
    in_stock_only: bool = False,
    multi_site_only: bool = True,   # default: only show products on 2+ sites
) -> Any:
    """
    The main price comparison listing — shows products sold on multiple sites
    with their min/max/avg prices and cheapest site.

    Designed for the price comparison dashboard.
    """
    from django.db.models import Avg, Count, Max, Min, Q

    qs = MasterProduct.objects.filter(status="active")

    if brand:
        qs = qs.filter(brand__icontains=brand)
    if category:
        qs = qs.filter(category__icontains=category)
    if name:
        qs = qs.filter(name__icontains=name)

    # Annotate with site count and price stats
    qs = qs.annotate(
        site_count=Count("site_products", distinct=True),
        current_min=Min("site_products__current_price"),
        current_max=Max("site_products__current_price"),
        current_avg=Avg("site_products__current_price"),
        in_stock_sites=Count(
            "site_products",
            filter=Q(site_products__in_stock=True),
            distinct=True,
        ),
    )

    if multi_site_only:
        qs = qs.filter(site_count__gte=2)
    if in_stock_only:
        qs = qs.filter(in_stock_sites__gte=1)

    qs = qs.order_by("brand", "name")

    # Build response — fetch site products for each master
    results = []
    async for master in qs:
        site_products = [
            sp async for sp in
            SiteProduct.objects.filter(master_product_id=master.id)
            .select_related("site")
            .order_by("current_price")
        ]

        priced      = [sp for sp in site_products if sp.current_price is not None]
        in_stock    = [sp for sp in priced if sp.in_stock]
        pool        = in_stock if in_stock else priced
        cheapest    = pool[0] if pool else None

        results.append({
            "master_product_id": master.id,
            "name":             master.name,
            "brand":            master.brand,
            "ean":              master.ean,
            "category":         master.category,
            "price_min":        min((sp.current_price for sp in pool), default=None),
            "price_max":        max((sp.current_price for sp in pool), default=None),
            "price_avg":        round(sum(sp.current_price for sp in pool) / len(pool), 2) if pool else None,
            "price_currency":   priced[0].currency if priced else "MAD",
            "sites_count":      len(site_products),
            "in_stock_count":   sum(1 for sp in site_products if sp.in_stock),
            "cheapest_site":    cheapest.site.domain if cheapest else None,
            "cheapest_url":     cheapest.product_url if cheapest else None,
            "sites":            [
                SitePriceOut(
                    site_id=sp.site_id,
                    site_name=sp.site.name,
                    site_domain=sp.site.domain,
                    price=sp.current_price,
                    currency=sp.currency,
                    in_stock=sp.in_stock,
                    product_url=sp.product_url,
                    last_scraped_at=sp.last_scraped_at,
                )
                for sp in site_products
            ],
        })

    return results


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
