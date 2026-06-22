"""
src/backend/grossiste/api.py
==============================
REST API endpoints for the grossiste integration.

Called by the external ERP system with credentials in the request payload.
Credentials are used on-the-fly and never stored.

Endpoints:
    POST /api/v1/grossiste/check-availability/
    POST /api/v1/grossiste/order/
    GET  /api/v1/grossiste/configs/          — list available grossiste configs
"""

from __future__ import annotations

import asyncio
from typing import Any

from ninja import Router, Schema
from ninja.security import HttpBearer

from .client import GrossisteAPIError, GrossisteAuthError, GrossisteClient
from .models import GrossisteConfig, GrossisteOrder, GrossisteProduct

router = Router()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class AvailabilityRequest(Schema):
    """
    Payload from ERP to check product availability.
    Credentials are passed per-request — never stored.
    """
    grossiste_name: str         # e.g. "GPM"
    username:       str         # grossiste login
    password:       str         # grossiste password
    product_code:   str         # CODE_PRODU e.g. "5230"


class AvailabilityResponse(Schema):
    grossiste:    str
    product_code: str
    product_name: str | None
    in_stock:     bool
    prix_pharmacien: float | None


class OrderRequest(Schema):
    """
    Payload from ERP to place a purchase order.
    Credentials are passed per-request — never stored.

    ⚠️  Order endpoint and payload structure are TBD.
    This skeleton accepts the request, logs it, and returns a placeholder.
    """
    grossiste_name: str
    username:       str
    password:       str
    product_code:   str
    quantity:       int = 1
    notes:          str = ""


class OrderResponse(Schema):
    order_id:         int | None
    grossiste:        str
    product_code:     str
    product_name:     str | None
    quantity:         int
    unit_price:       float | None
    status:           str
    external_order_id: str
    message:          str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/check-availability/",
    response=AvailabilityResponse,
    summary="Check product availability at a grossiste",
)
def check_availability(request, payload: AvailabilityRequest) -> dict[str, Any]:
    """
    Check whether a product is in stock at a grossiste.

    Credentials are used on-the-fly to login and make the API call.
    They are never persisted to the database.

    Called by the external ERP with:
        {
            "grossiste_name": "GPM",
            "username": "user@example.com",
            "password": "secret",
            "product_code": "5230"
        }
    """
    try:
        config = GrossisteConfig.objects.get(name=payload.grossiste_name, is_active=True)
    except GrossisteConfig.DoesNotExist:
        return {"error": f"Grossiste '{payload.grossiste_name}' not found or inactive"}

    # Get product name from our DB if we have it
    product_name = None
    prix = None
    try:
        gp = GrossisteProduct.objects.get(grossiste=config, code=payload.product_code)
        product_name = gp.name
        prix = float(gp.prix_pharmacien) if gp.prix_pharmacien else None
    except GrossisteProduct.DoesNotExist:
        pass

    try:
        in_stock = asyncio.run(_check_availability(config, payload))
    except GrossisteAuthError as exc:
        raise ValueError(f"Authentication failed: {exc}")
    except GrossisteAPIError as exc:
        raise ValueError(f"API error: {exc}")

    # Update our local record if we have it
    if product_name is not None:
        from django.utils import timezone
        GrossisteProduct.objects.filter(
            grossiste=config, code=payload.product_code
        ).update(in_stock=in_stock, availability_checked_at=timezone.now())

    return {
        "grossiste":        payload.grossiste_name,
        "product_code":     payload.product_code,
        "product_name":     product_name,
        "in_stock":         in_stock,
        "prix_pharmacien":  prix,
    }


@router.post(
    "/order/",
    response=OrderResponse,
    summary="Place a purchase order at a grossiste (SKELETON)",
)
def place_order(request, payload: OrderRequest) -> dict[str, Any]:
    """
    Place a purchase order at a grossiste.

    ⚠️  SKELETON — the actual grossiste order API endpoint and payload
    structure are not yet known. This endpoint:
      1. Validates the request
      2. Creates a GrossisteOrder record (status=DRAFT)
      3. Calls the order skeleton (returns placeholder)
      4. Updates the order record

    Once the actual API is documented, update GrossisteClient.place_order().

    Called by the external ERP with:
        {
            "grossiste_name": "GPM",
            "username": "user@example.com",
            "password": "secret",
            "product_code": "5230",
            "quantity": 10,
            "notes": "Urgent restock"
        }
    """
    try:
        config = GrossisteConfig.objects.get(name=payload.grossiste_name, is_active=True)
    except GrossisteConfig.DoesNotExist:
        raise ValueError(f"Grossiste '{payload.grossiste_name}' not found or inactive")

    # Get product from our DB
    try:
        product = GrossisteProduct.objects.get(grossiste=config, code=payload.product_code)
    except GrossisteProduct.DoesNotExist:
        raise ValueError(
            f"Product '{payload.product_code}' not found in {payload.grossiste_name} catalogue. "
            f"Load the catalogue first."
        )

    # Create order record
    order = GrossisteOrder.objects.create(
        grossiste=config,
        product=product,
        quantity=payload.quantity,
        unit_price=product.prix_pharmacien,
        status=GrossisteOrder.Status.DRAFT,
        notes=payload.notes,
    )

    # Attempt to submit (skeleton)
    try:
        result = asyncio.run(_place_order(config, payload, product))
        from django.utils import timezone
        order.status           = GrossisteOrder.Status.SUBMITTED
        order.response_payload = result
        order.submitted_at     = timezone.now()
        order.external_order_id = result.get("external_order_id", "")
        order.save(update_fields=["status", "response_payload", "submitted_at", "external_order_id"])
        message = result.get("message", "Order submitted (skeleton)")
    except (GrossisteAuthError, GrossisteAPIError) as exc:
        order.status        = GrossisteOrder.Status.FAILED
        order.error_message = str(exc)
        order.save(update_fields=["status", "error_message"])
        message = f"Order failed: {exc}"

    return {
        "order_id":          order.pk,
        "grossiste":         payload.grossiste_name,
        "product_code":      payload.product_code,
        "product_name":      product.name,
        "quantity":          payload.quantity,
        "unit_price":        float(product.prix_pharmacien) if product.prix_pharmacien else None,
        "status":            order.status,
        "external_order_id": order.external_order_id,
        "message":           message,
    }


@router.get(
    "/configs/",
    summary="List available grossiste configurations",
)
def list_configs(request) -> list[dict]:
    """
    Returns available grossiste configs (domain + paths only, no credentials).
    Used by the ERP to know which grossiste names to use in requests.
    """
    return [
        {
            "name":          c.name,
            "domain":        c.domain,
            "is_active":     c.is_active,
            "last_sync_at":  c.last_sync_at.isoformat() if c.last_sync_at else None,
            "product_count": c.products.count(),
        }
        for c in GrossisteConfig.objects.filter(is_active=True)
    ]


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

async def _check_availability(config: GrossisteConfig, payload: AvailabilityRequest) -> bool:
    async with GrossisteClient(config, payload.username, payload.password) as client:
        await client.login()
        return await client.check_availability(payload.product_code)


async def _place_order(
    config: GrossisteConfig,
    payload: OrderRequest,
    product: GrossisteProduct,
) -> dict:
    async with GrossisteClient(config, payload.username, payload.password) as client:
        await client.login()
        return await client.place_order(
            product_code=payload.product_code,
            quantity=payload.quantity,
            notes=payload.notes,
        )
