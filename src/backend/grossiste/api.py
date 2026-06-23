"""
src/backend/grossiste/api.py
==============================
REST API endpoints for the grossiste integration.

All calls are proxied to api.pharma.sobrus.com using the user's
Sobrus session cookie. No credentials are stored — the cookie is
passed per-request from the frontend/ERP.

Endpoints:
    POST /api/v1/grossiste/check-availability/
    POST /api/v1/grossiste/order/
    GET  /api/v1/grossiste/configs/
    POST /api/v1/grossiste/sync-sobrus-ids/
"""

from __future__ import annotations

import asyncio
from typing import Any

from ninja import Router, Schema

from .client import SobrusAPIError, SobrusAuthError, SobrusClient
from .models import GrossisteConfig, GrossisteOrder, GrossisteProduct

router = Router()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AvailabilityRequest(Schema):
    """
    Payload from ERP/frontend to check product availability.

    The sobrus_cookie is the full cookie string from the user's browser session.
    It is used on-the-fly and never stored.
    """
    grossiste_name:    str        # e.g. "GPM"
    sobrus_product_id: int        # Sobrus internal product ID (e.g. 148194)
    sobrus_cookie:     str        # "current_country_code=ma; SBSID2=..."
    csrf_token:        str = ""   # X-CSRF-TOKEN header (optional but recommended)


class AvailabilityResponse(Schema):
    grossiste:         str
    sobrus_product_id: int
    product_name:      str | None
    supplier_id:       int | None
    is_available:      bool
    prix_pharmacien:   float | None
    raw_response:      dict


class OrderRequest(Schema):
    """
    Payload from ERP/frontend to place a purchase order.
    """
    grossiste_name:    str
    sobrus_product_id: int
    quantity:          int = 1
    unit_price:        float | None = None   # prix_pharmacien — we fill from DB if not provided
    sale_price:        float | None = None   # retail price — we fill from DB if not provided
    tax_id:            int = 35
    owner_id:          str = ""
    notes:             str = ""
    sobrus_cookie:     str        # passed through, never stored
    csrf_token:        str = ""


class OrderResponse(Schema):
    order_id:          int | None
    grossiste:         str
    sobrus_product_id: int
    product_name:      str | None
    quantity:          int
    unit_price:        float | None
    status:            str
    message:           str
    raw_response:      dict


class SyncSobrusIdsRequest(Schema):
    """Trigger sync of Sobrus product IDs for a grossiste."""
    grossiste_name: str
    sobrus_cookie:  str
    csrf_token:     str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/check-availability/",
    response=AvailabilityResponse,
    summary="Check product availability via Sobrus API",
)
def check_availability(request, payload: AvailabilityRequest) -> dict[str, Any]:
    """
    Check whether a product is in stock at a grossiste.

    Proxies to:
      POST api.pharma.sobrus.com/purchaseorders/check-availability
        ?supplier_id={config.sobrus_supplier_id}&products={sobrus_product_id}

    The Sobrus session cookie is used on-the-fly and never stored.

    Example request:
        {
            "grossiste_name": "GPM",
            "sobrus_product_id": 148194,
            "sobrus_cookie": "current_country_code=ma; SBSID2=abc...",
            "csrf_token": "1133d8bd..."
        }
    """
    try:
        config = GrossisteConfig.objects.get(name=payload.grossiste_name, is_active=True)
    except GrossisteConfig.DoesNotExist:
        raise ValueError(f"Grossiste '{payload.grossiste_name}' not found or inactive.")

    if not config.sobrus_supplier_id:
        raise ValueError(
            f"Grossiste '{payload.grossiste_name}' has no sobrus_supplier_id configured. "
            f"Add it in Admin → Grossiste Configs."
        )

    # Get local product info if we have it
    product = GrossisteProduct.objects.filter(
        grossiste=config,
        sobrus_product_id=payload.sobrus_product_id,
    ).first()

    try:
        raw = asyncio.run(_check_availability(config, payload))
    except SobrusAuthError as exc:
        raise ValueError(f"Sobrus session expired: {exc}")
    except SobrusAPIError as exc:
        raise ValueError(f"Sobrus API error: {exc}")

    is_available = raw.get("isAvailable", False)

    # Update local record if we have it
    if product:
        from django.utils import timezone
        GrossisteProduct.objects.filter(pk=product.pk).update(
            in_stock=is_available,
            availability_checked_at=timezone.now(),
        )

    return {
        "grossiste":         payload.grossiste_name,
        "sobrus_product_id": payload.sobrus_product_id,
        "product_name":      product.name if product else None,
        "supplier_id":       config.sobrus_supplier_id,
        "is_available":      is_available,
        "prix_pharmacien":   float(product.prix_pharmacien) if product and product.prix_pharmacien else None,
        "raw_response":      raw,
    }


@router.post(
    "/order/",
    response=OrderResponse,
    summary="Place a purchase order via Sobrus API (skeleton)",
)
def place_order(request, payload: OrderRequest) -> dict[str, Any]:
    """
    Place a purchase order at a grossiste via Sobrus.

    ⚠️  SKELETON — the actual Sobrus order endpoint needs to be confirmed.
    Inspect F12 when placing an order in the Sobrus Pharma app.

    Example request:
        {
            "grossiste_name": "GPM",
            "sobrus_product_id": 148194,
            "quantity": 10,
            "notes": "Urgent restock",
            "sobrus_cookie": "current_country_code=ma; SBSID2=abc...",
            "csrf_token": "1133d8bd..."
        }
    """
    try:
        config = GrossisteConfig.objects.get(name=payload.grossiste_name, is_active=True)
    except GrossisteConfig.DoesNotExist:
        raise ValueError(f"Grossiste '{payload.grossiste_name}' not found or inactive.")

    if not config.sobrus_supplier_id:
        raise ValueError(f"Grossiste '{payload.grossiste_name}' has no sobrus_supplier_id.")

    # Get local product — use its prices if not provided in request
    product = GrossisteProduct.objects.filter(
        grossiste=config,
        sobrus_product_id=payload.sobrus_product_id,
    ).first()

    unit_price = payload.unit_price or (
        float(product.prix_pharmacien) if product and product.prix_pharmacien else None
    )
    sale_price = payload.sale_price or (
        float(product.ppm) if product and product.ppm else None
    )

    # Create order record
    order = GrossisteOrder.objects.create(
        grossiste=config,
        product=product,
        quantity=payload.quantity,
        unit_price=unit_price,
        sale_price=sale_price,
        status=GrossisteOrder.Status.DRAFT,
        notes=payload.notes,
    )

    try:
        raw = asyncio.run(_place_order(config, payload, unit_price, sale_price))
        from django.utils import timezone

        # Extract Sobrus order ID and transaction number from response
        order_data = raw.get("data", raw)
        sobrus_order_id   = str(order_data.get("ID", ""))
        transaction_num   = order_data.get("transaction_number", "")
        sobrus_status     = order_data.get("status", {}).get("ID", "") if isinstance(
            order_data.get("status"), dict) else str(order_data.get("status", ""))

        order.status               = GrossisteOrder.Status.SUBMITTED
        order.response_payload     = raw
        order.submitted_at         = timezone.now()
        order.sobrus_order_id      = sobrus_order_id
        order.sobrus_transaction_num = transaction_num
        order.sobrus_status        = sobrus_status
        order.save(update_fields=[
            "status", "response_payload", "submitted_at",
            "sobrus_order_id", "sobrus_transaction_num", "sobrus_status",
        ])
        message = f"Order {transaction_num or sobrus_order_id} created — status: {sobrus_status}"

    except (SobrusAuthError, SobrusAPIError) as exc:
        order.status        = GrossisteOrder.Status.FAILED
        order.error_message = str(exc)
        order.save(update_fields=["status", "error_message"])
        raw     = {}
        message = f"Failed: {exc}"

    return {
        "order_id":          order.pk,
        "grossiste":         payload.grossiste_name,
        "sobrus_product_id": payload.sobrus_product_id,
        "product_name":      product.name if product else None,
        "quantity":          payload.quantity,
        "unit_price":        unit_price,
        "status":            order.status,
        "message":           message,
        "raw_response":      raw,
    }


@router.get(
    "/configs/",
    summary="List available grossiste configs",
)
def list_configs(request) -> list[dict]:
    """Returns available grossiste configs (no credentials)."""
    return [
        {
            "name":               c.name,
            "domain":             c.domain,
            "sobrus_supplier_id": c.sobrus_supplier_id,
            "is_active":          c.is_active,
            "last_sync_at":       c.last_sync_at.isoformat() if c.last_sync_at else None,
            "product_count":      c.products.count(),
            "sobrus_id_count":    c.products.filter(sobrus_product_id__isnull=False).count(),
        }
        for c in GrossisteConfig.objects.filter(is_active=True)
    ]


@router.post(
    "/sync-sobrus-ids/",
    summary="Sync Sobrus product IDs for a grossiste",
)
def sync_sobrus_ids(request, payload: SyncSobrusIdsRequest) -> dict:
    """
    Fetch Sobrus internal product IDs and store them on GrossisteProduct records.
    Must be run after load_grossiste_file to enable availability checks.

    ⚠️  Endpoint TBD — inspect F12 in the Sobrus app when browsing
    a supplier's product catalogue in the purchase order screen.
    """
    try:
        config = GrossisteConfig.objects.get(name=payload.grossiste_name, is_active=True)
    except GrossisteConfig.DoesNotExist:
        raise ValueError(f"Grossiste '{payload.grossiste_name}' not found.")

    if not config.sobrus_supplier_id:
        raise ValueError(f"Grossiste '{payload.grossiste_name}' has no sobrus_supplier_id.")

    try:
        result = asyncio.run(_sync_sobrus_ids(config, payload))
        return {"status": "ok", "updated": result["updated"], "not_found": result["not_found"]}
    except (SobrusAuthError, SobrusAPIError) as exc:
        raise ValueError(str(exc))


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

async def _check_availability(config: GrossisteConfig, payload: AvailabilityRequest) -> dict:
    async with SobrusClient(payload.sobrus_cookie, payload.csrf_token) as client:
        return await client.check_availability(
            supplier_id=config.sobrus_supplier_id,
            sobrus_product_id=payload.sobrus_product_id,
        )


async def _place_order(
    config: GrossisteConfig,
    payload: OrderRequest,
    unit_price: float | None,
    sale_price: float | None,
) -> dict:
    async with SobrusClient(payload.sobrus_cookie, payload.csrf_token) as client:
        return await client.place_order(
            supplier_id=config.sobrus_supplier_id,
            sobrus_product_id=payload.sobrus_product_id,
            quantity=payload.quantity,
            unit_price=unit_price,
            sale_price=sale_price,
            tax_id=payload.tax_id,
            owner_id=payload.owner_id,
            notes=payload.notes,
        )


async def _sync_sobrus_ids(config: GrossisteConfig, payload: SyncSobrusIdsRequest) -> dict:
    """Fetch Sobrus product list and match to local GrossisteProducts by name/code."""
    async with SobrusClient(payload.sobrus_cookie, payload.csrf_token) as client:
        products = await client.fetch_supplier_products(config.sobrus_supplier_id)

    updated = not_found = 0
    for p in products:
        sobrus_id = p.get("id")
        code      = str(p.get("code") or p.get("reference") or "").strip()
        if not sobrus_id:
            continue
        rows = await GrossisteProduct.objects.filter(
            grossiste=config, code=code
        ).aupdate(sobrus_product_id=sobrus_id)
        if rows:
            updated += 1
        else:
            not_found += 1

    return {"updated": updated, "not_found": not_found}
