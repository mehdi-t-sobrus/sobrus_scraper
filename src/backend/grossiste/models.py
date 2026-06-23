"""
src/backend/grossiste/models.py
================================
Django models for the grossiste (wholesale distributor) integration.

Architecture:
  GrossisteConfig    — one row per distributor (domain, Sobrus supplier ID)
  GrossisteProduct   — wholesale product linked to MasterProduct
  GrossisteOrder     — purchase order record

Availability + ordering flow:
  All calls go through api.pharma.sobrus.com — we never call the grossiste
  directly. The Sobrus session cookie is passed through per-request from
  the frontend. No credentials are stored here.

  POST api.pharma.sobrus.com/purchaseorders/check-availability
    ?supplier_id={config.sobrus_supplier_id}&products={product.sobrus_product_id}
  Cookie: {sobrus_session_cookie}
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class GrossisteConfig(models.Model):
    """
    Configuration for one grossiste distributor.
    Stores domain, API paths, and Sobrus supplier ID.
    NO credentials — auth handled by Sobrus session cookie passed per-request.
    """

    name = models.CharField(
        max_length=128,
        unique=True,
        help_text=_("Identifier, e.g. 'GPM', 'Sophasais', 'Lodimed'"),
    )
    domain = models.URLField(
        max_length=256,
        unique=True,
        help_text=_("Base URL of the grossiste site, e.g. https://gpm.ma"),
    )
    is_active = models.BooleanField(default=True)

    # Sobrus Pharma internal supplier ID
    # Used in api.pharma.sobrus.com/purchaseorders/check-availability?supplier_id=X
    sobrus_supplier_id = models.IntegerField(
        null=True, blank=True,
        help_text=_(
            "Sobrus internal supplier ID. "
            "GPM=1, Sophasais=1570, Lodimed=346"
        ),
    )

    # API path overrides (kept for direct grossiste access if ever needed)
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
    A product from a grossiste catalogue.
    Parallel to SiteProduct — both hang off MasterProduct but represent
    different sides of the market (wholesale vs retail).

    Fields from extracted JS catalogue:
      CODE_PRODU  → code
      NOM_PRODUI  → name
      PRIX_PHAR   → prix_pharmacien
      PPM         → ppm
      FORME_PROD  → forme
      PA          → pa

    sobrus_product_id is fetched from the Sobrus API and is required
    for check-availability and order calls.
    """

    grossiste = models.ForeignKey(
        GrossisteConfig,
        on_delete=models.CASCADE,
        related_name="products",
    )
    master_product = models.ForeignKey(
        "products.MasterProduct",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="grossiste_products",
        help_text=_("Linked MasterProduct — populated by the matching pipeline."),
    )

    # Raw fields from grossiste catalogue
    code  = models.CharField(max_length=32, db_index=True,
                             help_text=_("CODE_PRODU"))
    name  = models.CharField(max_length=512, help_text=_("NOM_PRODUI"))
    prix_pharmacien = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True,
        help_text=_("PRIX_PHAR — pharmacy buy price (MAD)"),
    )
    ppm = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=_("PPM — prix public maximum (MAD)"),
    )
    pa = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True,
        help_text=_("PA — prix d'achat (often empty)"),
    )
    forme = models.CharField(max_length=8, blank=True, default="",
                             help_text=_("FORME_PROD — dosage form code"))

    # Sobrus internal product ID — required for API calls
    sobrus_product_id = models.IntegerField(
        null=True, blank=True,
        db_index=True,
        help_text=_(
            "Sobrus internal product ID used in "
            "api.pharma.sobrus.com/purchaseorders/check-availability. "
            "Fetched via Sobrus API."
        ),
    )

    # Match metadata
    match_confidence  = models.FloatField(null=True, blank=True)
    manually_verified = models.BooleanField(default=False)

    # Availability — refreshed via Sobrus API on demand
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
            models.Index(fields=["code"],            name="idx_gros_code"),
            models.Index(fields=["name"],            name="idx_gros_name"),
            models.Index(fields=["grossiste", "in_stock"], name="idx_gros_stock"),
            models.Index(fields=["master_product"],  name="idx_gros_master"),
            models.Index(fields=["sobrus_product_id"], name="idx_gros_sobrus_id"),
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
    Purchase order placed via api.pharma.sobrus.com/purchaseorders/create.
    """

    class Status(models.TextChoices):
        DRAFT     = "draft",     _("Draft — not yet submitted")
        SUBMITTED = "submitted", _("Submitted to Sobrus")
        CONFIRMED = "confirmed", _("Confirmed by grossiste")
        FAILED    = "failed",    _("Submission failed")

    grossiste  = models.ForeignKey(GrossisteConfig, on_delete=models.PROTECT, related_name="orders")
    product    = models.ForeignKey(GrossisteProduct, on_delete=models.PROTECT,
                                   related_name="orders", null=True, blank=True)
    quantity   = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True,
                                     help_text=_("Prix pharmacien / purchase price at time of order"))
    sale_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                     help_text=_("Retail sale price at time of order"))
    status     = models.CharField(max_length=16, choices=Status.choices,
                                  default=Status.DRAFT, db_index=True)

    # Sobrus order details (populated after successful creation)
    sobrus_order_id        = models.CharField(max_length=64, blank=True, default="",
                                              help_text=_("Sobrus internal order ID (e.g. 9757341)"))
    sobrus_transaction_num = models.CharField(max_length=64, blank=True, default="",
                                              help_text=_("Sobrus transaction number (e.g. BC-3579)"))
    sobrus_status          = models.CharField(max_length=64, blank=True, default="",
                                              help_text=_("Sobrus order status (e.g. approved)"))

    response_payload = models.JSONField(null=True, blank=True,
                                        help_text=_("Full Sobrus API response"))
    error_message    = models.TextField(blank=True, default="")
    notes            = models.TextField(blank=True, default="")

    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "Grossiste Order"
        verbose_name_plural = "Grossiste Orders"
        ordering            = ["-created_at"]

    def __str__(self) -> str:
        ref = self.sobrus_transaction_num or f"#{self.pk}"
        name = self.product.name if self.product else "Unknown product"
        return f"{ref} — {name} x{self.quantity} [{self.status}]"

    @property
    def total_price(self):
        if self.unit_price is not None:
            return self.unit_price * self.quantity
        return None
