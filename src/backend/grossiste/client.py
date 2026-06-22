"""
src/backend/grossiste/client.py
================================
HTTP client for grossiste distributor APIs.

All 3 distributors share the same API structure:
  POST  /login           → session cookie
  GET   /GetProd         → full product list (JavaScript var products = [...])
  GET   /GetProd/{code}  → single product availability (in_stock boolean)
  POST  /order           → place order (SKELETON — endpoint TBD)

Usage:
    from grossiste.client import GrossisteClient
    from grossiste.models import GrossisteConfig

    config = GrossisteConfig.objects.get(name="GPM")
    async with GrossisteClient(config) as client:
        await client.login()
        products = await client.fetch_product_list()
        is_available = await client.check_availability("5230")
        order = await client.place_order("5230", quantity=10)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)


class GrossisteAuthError(Exception):
    """Raised when login fails."""


class GrossisteAPIError(Exception):
    """Raised when an API call returns an unexpected response."""


class GrossisteClient:
    """
    Async HTTP client for one grossiste distributor.

    Credentials are passed per-call from the external ERP system —
    they are NEVER stored in the database or on disk.

    Parameters
    ----------
    config:
        GrossisteConfig — domain + API paths only.
    username:
        Grossiste login username (from ERP request payload).
    password:
        Grossiste login password (from ERP request payload).
    """

    def __init__(self, config, username: str, password: str) -> None:
        self.config   = config
        self.username = username
        self.password = password
        self.base     = config.domain.rstrip("/")
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "GrossisteClient":
        self._session = AsyncSession(impersonate="chrome", timeout=30)
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("Use 'async with GrossisteClient(config, user, pwd) as client:'")
        return self._session

    # -------------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------------

    async def login(self) -> None:
        """
        POST credentials to the login endpoint and store the session cookie.
        Credentials come from the constructor — passed through from ERP payload.
        Raises GrossisteAuthError if login fails.
        """
        url = f"{self.base}{self.config.login_path}"
        logger.info("[%s] Logging in as %s...", self.config.name, self.username)

        resp = await self.session.post(
            url,
            data={
                "username": self.username,
                "password": self.password,
            },
        )

        # Treat any non-2xx or redirect back to login as failure
        if resp.status_code not in (200, 201, 302):
            raise GrossisteAuthError(
                f"[{self.config.name}] Login failed — HTTP {resp.status_code}"
            )

        # Some sites redirect to dashboard on success, back to /login on failure
        if self.config.login_path in resp.url:
            raise GrossisteAuthError(
                f"[{self.config.name}] Login failed — redirected back to login page"
            )

        logger.info("[%s] Login successful.", self.config.name)

    # -------------------------------------------------------------------------
    # Product catalogue
    # -------------------------------------------------------------------------

    async def fetch_product_list(self) -> list[dict[str, Any]]:
        """
        Fetch the full product catalogue.

        The API returns a JavaScript page containing:
            var products = [{CODE_PRODU, NOM_PRODUI, PRIX_PHAR, PPM, FORME_PROD, PA}, ...]

        Returns
        -------
        list[dict]
            Parsed product records ready for DB upsert.
        """
        url = f"{self.base}{self.config.products_path}"
        logger.info("[%s] Fetching product catalogue...", self.config.name)

        resp = await self.session.get(url)
        if resp.status_code != 200:
            raise GrossisteAPIError(
                f"[{self.config.name}] Product list returned HTTP {resp.status_code}"
            )

        products = self._parse_product_list(resp.text)
        logger.info("[%s] Fetched %d products.", self.config.name, len(products))
        return products

    def _parse_product_list(self, html: str) -> list[dict[str, Any]]:
        """
        Extract the products JSON array from the JavaScript page.

        Handles: var products = [...];
        """
        match = re.search(r"var\s+products\s*=\s*(\[.*?\]);", html, re.DOTALL)
        if not match:
            raise GrossisteAPIError(
                f"[{self.config.name}] Could not find 'var products = [...]' in response. "
                f"Response length: {len(html)} chars. "
                f"First 500 chars: {html[:500]}"
            )

        try:
            raw = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise GrossisteAPIError(
                f"[{self.config.name}] Failed to parse products JSON: {exc}"
            ) from exc

        # Normalise field names to our internal snake_case
        products = []
        for item in raw:
            prix = item.get("PRIX_PHAR") or "0"
            ppm  = item.get("PPM") or "0"
            pa   = item.get("PA") or ""
            products.append({
                "code":             str(item.get("CODE_PRODU", "")).strip(),
                "name":             str(item.get("NOM_PRODUI", "")).strip(),
                "prix_pharmacien":  self._parse_decimal(prix),
                "ppm":              self._parse_decimal(ppm),
                "pa":               self._parse_decimal(pa) if pa else None,
                "forme":            str(item.get("FORME_PROD", "")).strip(),
            })

        return [p for p in products if p["code"]]  # skip empty codes

    @staticmethod
    def _parse_decimal(value: str) -> float | None:
        """Parse a price string to float, returning None if invalid or zero."""
        try:
            f = float(str(value).replace(",", ".").strip())
            return f if f > 0 else None
        except (ValueError, TypeError):
            return None

    # -------------------------------------------------------------------------
    # Availability check
    # -------------------------------------------------------------------------

    async def check_availability(self, product_code: str) -> bool:
        """
        Check whether a single product is in stock.

        Calls GET /GetProd/{code} and interprets the boolean response.

        Parameters
        ----------
        product_code:
            The CODE_PRODU value (e.g. "5230").

        Returns
        -------
        bool
            True if in stock, False if out of stock.
        """
        url = f"{self.base}{self.config.products_path}/{product_code}"
        logger.debug("[%s] Checking availability for %s...", self.config.name, product_code)

        resp = await self.session.get(url)

        if resp.status_code == 404:
            logger.warning("[%s] Product %s not found (404).", self.config.name, product_code)
            return False

        if resp.status_code != 200:
            raise GrossisteAPIError(
                f"[{self.config.name}] Availability check for {product_code} "
                f"returned HTTP {resp.status_code}"
            )

        return self._parse_availability(resp.text, product_code)

    def _parse_availability(self, body: str, product_code: str) -> bool:
        """
        Parse the availability response.

        Expected responses (adjust once actual API is tested):
          - JSON boolean:    true / false
          - JSON object:     {"inStock": true} or {"disponible": 1}
          - Plain text:      "1" / "0" or "true" / "false"
        """
        body = body.strip()

        # Try JSON first
        try:
            data = json.loads(body)
            if isinstance(data, bool):
                return data
            if isinstance(data, int):
                return data > 0
            if isinstance(data, dict):
                # Look for common boolean keys
                for key in ("inStock", "in_stock", "disponible", "available", "stock"):
                    if key in data:
                        val = data[key]
                        return bool(val) if not isinstance(val, str) else val.lower() == "true"
        except json.JSONDecodeError:
            pass

        # Plain text fallback
        lower = body.lower()
        if lower in ("true", "1", "yes", "oui", "disponible"):
            return True
        if lower in ("false", "0", "no", "non", "indisponible"):
            return False

        logger.warning(
            "[%s] Unexpected availability response for %s: %r — defaulting to False",
            self.config.name, product_code, body[:100],
        )
        return False

    # -------------------------------------------------------------------------
    # Order placement (SKELETON — endpoint and payload TBD)
    # -------------------------------------------------------------------------

    async def place_order(
        self,
        product_code: str,
        quantity: int = 1,
        notes: str = "",
    ) -> dict[str, Any]:
        """
        Place a purchase order with the grossiste.

        ⚠️  SKELETON — the actual endpoint URL and request payload are not yet
        known. This method logs the intent and returns a placeholder response.
        Replace the body once the API is documented.

        Parameters
        ----------
        product_code:
            The CODE_PRODU to order.
        quantity:
            Number of units to order.
        notes:
            Optional order notes.

        Returns
        -------
        dict
            API response (or placeholder when endpoint is TBD).
        """
        url = f"{self.base}{self.config.order_path}"

        # TODO: Replace with actual payload structure once API is documented
        payload = {
            "product_code": product_code,
            "quantity":     quantity,
            "notes":        notes,
            # "delivery_address": "...",  # TBD
            # "payment_method":   "...",  # TBD
        }

        logger.info(
            "[%s] SKELETON: Would POST order to %s — code=%s qty=%d",
            self.config.name, url, product_code, quantity,
        )
        logger.debug("[%s] Order payload (TBD): %s", self.config.name, payload)

        # TODO: Uncomment once endpoint is confirmed
        # resp = await self.session.post(url, json=payload)
        # if resp.status_code not in (200, 201):
        #     raise GrossisteAPIError(
        #         f"[{self.config.name}] Order failed — HTTP {resp.status_code}: {resp.text[:200]}"
        #     )
        # return resp.json()

        return {
            "status":    "skeleton",
            "message":   "Order endpoint not yet implemented — placeholder response",
            "would_send": payload,
            "to_url":    url,
        }
