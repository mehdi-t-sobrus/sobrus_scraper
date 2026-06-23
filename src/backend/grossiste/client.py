"""
src/backend/grossiste/client.py
================================
Client for the Sobrus Pharma API (api.pharma.sobrus.com).

All grossiste operations (availability checks, orders) go through Sobrus —
we never call the grossiste directly. The user's Sobrus session cookie is
passed through per-request and never stored.

Endpoints used:
  POST /purchaseorders/check-availability?supplier_id=X&products=Y
  POST /purchaseorders/          (order placement — TBD, to confirm)

Usage:
    from grossiste.client import SobrusClient, SobrusAPIError

    async with SobrusClient(sobrus_cookie) as client:
        result = await client.check_availability(
            supplier_id=1,         # config.sobrus_supplier_id
            sobrus_product_id=148194  # product.sobrus_product_id
        )
        # result = {"supplierId": 1, "isAvailable": True}
"""

from __future__ import annotations

import logging
from typing import Any

from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

SOBRUS_API_BASE = "https://api.pharma.sobrus.com"


class SobrusAPIError(Exception):
    """Raised when the Sobrus API returns an unexpected response."""


class SobrusAuthError(SobrusAPIError):
    """Raised when the Sobrus session cookie is invalid or expired."""


class SobrusClient:
    """
    Async HTTP client that proxies requests to api.pharma.sobrus.com.

    The Sobrus session cookie is passed per-request from the frontend —
    it is never stored in the database.

    Parameters
    ----------
    sobrus_cookie:
        The full cookie string from the user's Sobrus session, e.g.:
        "current_country_code=ma; SBSID2=ght49q17..."
    csrf_token:
        The X-CSRF-TOKEN header value from the Sobrus session.
    """

    def __init__(self, sobrus_cookie: str, csrf_token: str = "") -> None:
        self.sobrus_cookie = sobrus_cookie
        self.csrf_token    = csrf_token
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "SobrusClient":
        self._session = AsyncSession(impersonate="chrome", timeout=30)
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("Use 'async with SobrusClient(cookie) as client:'")
        return self._session

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept":          "application/json",
            "Content-Type":    "application/json",
            "Cookie":          self.sobrus_cookie,
            "Origin":          "https://app.pharma.sobrus.com",
            "Referer":         "https://app.pharma.sobrus.com/",
        }
        if self.csrf_token:
            headers["X-CSRF-TOKEN"] = self.csrf_token
        return headers

    # -------------------------------------------------------------------------
    # Availability check
    # -------------------------------------------------------------------------

    async def check_availability(
        self,
        supplier_id: int,
        sobrus_product_id: int,
    ) -> dict[str, Any]:
        """
        Check whether a product is available at a grossiste via Sobrus.

        POST /purchaseorders/check-availability
          ?supplier_id={supplier_id}&products={sobrus_product_id}

        Parameters
        ----------
        supplier_id:
            Sobrus internal supplier ID (e.g. GPM=1, Sophasais=1570, Lodimed=346).
        sobrus_product_id:
            Sobrus internal product ID (e.g. 148194).

        Returns
        -------
        dict
            e.g. {"supplierId": 363, "isAvailable": True}
        """
        url = (
            f"{SOBRUS_API_BASE}/purchaseorders/check-availability"
            f"?supplier_id={supplier_id}&products={sobrus_product_id}"
        )
        logger.info(
            "Checking availability: supplier=%s product=%s",
            supplier_id, sobrus_product_id,
        )

        resp = await self.session.post(url, json={}, headers=self._headers())

        if resp.status_code == 401:
            raise SobrusAuthError(
                "Sobrus session expired or invalid — the user needs to log in again."
            )
        if resp.status_code != 200:
            raise SobrusAPIError(
                f"check-availability returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception:
            raise SobrusAPIError(
                f"check-availability returned non-JSON: {resp.text[:200]}"
            )

        logger.info(
            "Availability result: supplier=%s product=%s → %s",
            supplier_id, sobrus_product_id, data,
        )
        return data

    # -------------------------------------------------------------------------
    # Order placement (SKELETON — endpoint to confirm)
    # -------------------------------------------------------------------------

    async def place_order(
        self,
        supplier_id: int,
        sobrus_product_id: int,
        quantity: int = 1,
        unit_price: float | None = None,
        sale_price: float | None = None,
        tax_id: int = 35,
        owner_id: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        """
        Place a purchase order via Sobrus.

        POST /purchaseorders/create

        Parameters
        ----------
        supplier_id:
            Sobrus internal supplier ID.
        sobrus_product_id:
            Sobrus internal product ID (the "ID" field in the payload).
        quantity:
            Number of units to order.
        unit_price:
            Purchase price per unit (PRIX_PHAR from grossiste catalogue).
        sale_price:
            Retail sale price (PPM or current retail price).
        tax_id:
            Tax ID — defaults to 35 (observed in real payload).
        owner_id:
            Sobrus user/owner ID. Taken from the authenticated session.
        notes:
            Optional order notes (maps to "comment").
        """
        from datetime import date

        url = f"{SOBRUS_API_BASE}/purchaseorders/create"

        payload = {
            "products": [
                {
                    "ID":                  sobrus_product_id,
                    "quantity":            quantity,
                    "unit_price":          str(unit_price) if unit_price else "0.00",
                    "unit_original_price": unit_price or 0,
                    "purchase_price":      unit_price or 0,
                    "sale_price":          sale_price or 0,
                    "tax_id":              tax_id,
                    "discount_type":       "percentage",
                    "discount":            "0.00",
                    "available":           -1,
                    "product_price_id":    "",
                }
            ],
            "products_details":                  [],
            "purchase_order_date":               date.today().isoformat(),
            "global_discount_application_type":  "each_product",
            "global_discount_type":              "percentage",
            "supplier_id":                       str(supplier_id),
            "contact_id":                        "",
            "orderOnline":                       "false",
            "owner_id":                          owner_id,
            "status_action":                     "approve",
            "comment":                           notes or None,
        }

        logger.info(
            "Placing order: supplier=%s product=%s qty=%d price=%s",
            supplier_id, sobrus_product_id, quantity, unit_price,
        )

        resp = await self.session.post(url, json=payload, headers=self._headers())

        if resp.status_code == 401:
            raise SobrusAuthError("Sobrus session expired or invalid.")
        if resp.status_code not in (200, 201):
            raise SobrusAPIError(
                f"Order creation failed — HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            create_result = resp.json()
        except Exception:
            raise SobrusAPIError(f"Order response was not JSON: {resp.text[:200]}")

        logger.info("Order created: %s", create_result)

        # Fetch full order details if we got an order ID back
        order_id = (
            create_result.get("data", {}).get("ID")
            or create_result.get("ID")
            or create_result.get("id")
        )
        if order_id:
            try:
                details = await self.get_order(order_id)
                return details
            except SobrusAPIError as exc:
                logger.warning("Could not fetch order details after creation: %s", exc)

        return create_result

    async def get_order(self, order_id: str | int) -> dict[str, Any]:
        """
        Fetch full order details after creation.
        GET /purchaseorders/{order_id}
        """
        url = f"{SOBRUS_API_BASE}/purchaseorders/{order_id}"
        logger.info("Fetching order details: %s", order_id)

        resp = await self.session.get(url, headers=self._headers())

        if resp.status_code == 401:
            raise SobrusAuthError("Sobrus session expired.")
        if resp.status_code != 200:
            raise SobrusAPIError(
                f"GET order {order_id} returned HTTP {resp.status_code}"
            )

        return resp.json()

    # -------------------------------------------------------------------------
    # Sync Sobrus product IDs
    # -------------------------------------------------------------------------

    async def fetch_supplier_products(self, supplier_id: int) -> list[dict[str, Any]]:
        """
        Fetch the product list for a supplier from the Sobrus API.
        Used to populate GrossisteProduct.sobrus_product_id.

        ⚠️  Endpoint TBD — inspect the Sobrus app to find how it loads
        the grossiste product catalogue. Look for a GET request when
        browsing a supplier's products in the Sobrus purchase order screen.
        """
        # TODO: Find and confirm the actual endpoint
        url = f"{SOBRUS_API_BASE}/purchaseorders/products?supplier_id={supplier_id}"

        logger.info("Fetching Sobrus product list for supplier %s...", supplier_id)

        resp = await self.session.get(url, headers=self._headers())

        if resp.status_code == 401:
            raise SobrusAuthError("Sobrus session expired.")
        if resp.status_code != 200:
            raise SobrusAPIError(
                f"Product list returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        return resp.json()
