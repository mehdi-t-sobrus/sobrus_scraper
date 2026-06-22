"""
src/backend/grossiste/models.py
================================
Django models for the grossiste (wholesale distributor) integration.

Architecture:
  GrossisteConfig    — credentials + domain per distributor (3 total)
  GrossisteProduct   — wholesale product linked to MasterProduct
                       Parallel to SiteProduct but for wholesale prices
  GrossisteOrder     — purchase order skeleton (endpoint TBD)

Pricing model:
  MasterProduct
  ├── SiteProduct       → retail prices  (what consumers pay on e-commerce sites)
  └── GrossisteProduct  → wholesale prices (what a seller pays to stock from grossiste)

This lets us answer:
  "I can buy ISDIN SPF50 from GPM at 55 MAD and it sells on cotepara for 270 MAD"
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


class GrossisteConfig(models.Model):
    """
    Configuration for one grossiste distributor.
    Stores domain and API paths only — NO credentials.

    Credentials (username + password) are passed per-request from the
    external ERP system and used on-the-fly. They are never stored here.
    """

    name = models.CharField(
        max_length=128,
        unique=True,
        help_text=_("Identifier used in API calls, e.g. 'GPM', 'COPHARM', 'SOMAPHARM'"),
    )
    domain = models.URLField(
        max_length=256,
        unique=True,
        help_text=_("Base URL, e.g. https://gpm.ma"),
    )
    is_active = models.BooleanField(default=True)

    # API path overrides (defaults work for all 3 known sites)
    login_path    = models.CharField(max_length=128, default="/login")
    products_path = models.CharField(max_length=128, default="/GetProd")
    order_path    = models.CharField(max_length=128, default="/order")

    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Grossiste Config"
        verbose_name_plural = "Grossiste Configs"
        ordering            = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.domain})"


class GrossisteProduct(models.Model):
    """
    A product from a grossiste catalogue, optionally linked to a MasterProduct.

    Parallel to SiteProduct — both hang off MasterProduct but represent
    different sides of the market:
      SiteProduct       → retail listing  (B2C price consumers pay)
      GrossisteProduct  → wholesale listing (B2B price sellers pay to stock)

    Fields map directly from the API response:
      CODE_PRODU  → code
      NOM_PRODUI  → name
      PRIX_PHAR   → prix_pharmacien (pharmacy buy price, excl. tax)
      PPM         → ppm (prix public maximum — max legal retail price)
      FORME_PROD  → forme (dosage form: CO=tablet, GE=capsule, SI=syrup…)
      PA          → pa (prix d'achat — purchase price, often empty)

    Matching:
      master_product is populated by the matching pipeline (same 6-tier
      approach as SiteProduct: EAN exact → name fuzzy → vector similarity).
      Until matched, master_product is NULL.
    """

    grossiste = models.ForeignKey(
        GrossisteConfig,
        on_delete=models.CASCADE,
        related_name="products",
    )
    master_product = models.ForeignKey(
        "products.MasterProduct",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="grossiste_products",
        help_text=_(
            "Linked MasterProduct — populated by the matching pipeline. "
            "NULL means not yet matched."
        ),
    )

    # Raw fields from grossiste API
    code  = models.CharField(
        max_length=32,
        db_index=True,
        help_text=_("CODE_PRODU — unique product code within this grossiste"),
    )
    name  = models.CharField(max_length=512, help_text=_("NOM_PRODUI"))
    prix_pharmacien = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True,
        help_text=_("PRIX_PHAR — pharmacy buy price (MAD excl. tax)"),
    )
    ppm = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=_("PPM — prix public maximum / max legal retail price (MAD)"),
    )
    pa = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True,
        help_text=_("PA — prix d'achat (often empty)"),
    )
    forme = models.CharField(
        max_length=8, blank=True, default="",
        help_text=_("FORME_PROD — dosage form code (CO, GE, SI, GO, SP, …)"),
    )

    # Match metadata
    match_confidence = models.FloatField(
        null=True, blank=True,
        help_text=_("Confidence score from the matching pipeline (0–1)."),
    )
    manually_verified = models.BooleanField(
        default=False,
        help_text=_("Tick after manually confirming the MasterProduct link is correct."),
    )

    # Availability — refreshed on demand via GetProd/{code}
    in_stock = models.BooleanField(
        null=True, blank=True, default=None,
        help_text=_("None = not yet checked"),
    )
    availability_checked_at = models.DateTimeField(null=True, blank=True)

    synced_at  = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "Grossiste Product"
        verbose_name_plural = "Grossiste Products"
        unique_together     = [("grossiste", "code")]
        ordering            = ["name"]
        indexes             = [
            models.Index(fields=["code"],               name="idx_gros_code"),
            models.Index(fields=["name"],               name="idx_gros_name"),
            models.Index(fields=["grossiste", "in_stock"], name="idx_gros_stock"),
            models.Index(fields=["master_product"],     name="idx_gros_master"),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name} [{self.grossiste.name}]"

    @property
    def forme_display(self) -> str:
        FORMES = {
            "CO": "Comprimé", "CP": "Comprimé", "GE": "Gélule",
            "GO": "Gouttes",  "SI": "Sirop",    "SP": "Spray",
            "PO": "Poudre",   "CR": "Crème",    "TU": "Tube",
            "AM": "Ampoule",  "LI": "Liquide",  "SO": "Solution",
            "EM": "Émulsion", "SU": "Suppositoire",
        }
        return FORMES.get(self.forme, self.forme)


class GrossisteOrder(models.Model):
    """
    Purchase order skeleton — endpoint and payload TBD.
    Created when a purchase is triggered from the Admin or API.
    """

    class Status(models.TextChoices):
        DRAFT     = "draft",     _("Draft — not yet submitted")
        SUBMITTED = "submitted", _("Submitted to grossiste")
        CONFIRMED = "confirmed", _("Confirmed by grossiste")
        FAILED    = "failed",    _("Submission failed")

    grossiste = models.ForeignKey(
        GrossisteConfig,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    product = models.ForeignKey(
        GrossisteProduct,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    quantity   = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True,
        help_text=_("Prix pharmacien at time of order"),
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )

    # Response from grossiste API (populated after submission)
    external_order_id = models.CharField(
        max_length=256, blank=True, default="",
        help_text=_("Order ID returned by the grossiste API"),
    )
    response_payload = models.JSONField(
        null=True, blank=True,
        help_text=_("Full API response for debugging"),
    )
    error_message = models.TextField(blank=True, default="")
    notes         = models.TextField(blank=True, default="")

    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name        = "Grossiste Order"
        verbose_name_plural = "Grossiste Orders"
        ordering            = ["-created_at"]

    def __str__(self) -> str:
        return f"Order #{self.pk} — {self.product.name} x{self.quantity} [{self.status}]"

    @property
    def total_price(self):
        if self.unit_price is not None:
            return self.unit_price * self.quantity
        return None
