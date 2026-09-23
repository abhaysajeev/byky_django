"""Vehicle Management.

SRS module 4, built from the legacy ERP.IMS area -- but only the vehicle
subset of it. IMS is a fork of an FMCG distribution product (CLAUDE.md
section 3), so most of its tables (ImsBatch, ImsFeatures, ImsStockAvailability,
...) are out of scope here; this app carries forward only what a vehicle rental
fleet actually needs.
"""

from django.db import models

from core.models import ApprovalMixin, TimeStampedModel


class Brand(ApprovalMixin, TimeStampedModel):
    """A vehicle brand.

    Legacy carried this (ImsBrand) but never used it: ManufacturerID was a
    required FK to ImsManufacturer, and both tables sat at 0 rows in QA, along
    with the two overlay tables built on top of them (ImsStockBrands,
    ImsStockModels). No legacy data to map -- built clean.

    `manufacturer` is free text, not a FK to its own master: legacy's
    ImsManufacturer was also always empty, so there is no reuse case yet to
    justify a separate table for it.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="brands"
    )
    brand_code = models.CharField("Brand Code", max_length=20)
    brand_name = models.CharField("Brand Name", max_length=100)
    description = models.TextField(blank=True)
    manufacturer = models.CharField(max_length=100, blank=True)
    website_link = models.URLField(blank=True)

    class Meta:
        db_table = "brand"
        ordering = ["brand_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "brand_code"],
                name="uniq_brand_code_per_company",
                violation_error_message="A brand with this code already exists.",
            ),
        ]
        indexes = [models.Index(fields=["company"])]

    def __str__(self):
        return self.brand_name


class Category(ApprovalMixin, TimeStampedModel):
    """A vehicle category (e.g. BYKY, Karty, Two Wheels).

    Legacy carried ImsCategory.ParentCategoryID (self-referencing hierarchy)
    and CategoryImage -- neither was ever populated across the 18 real rows,
    so neither is carried forward here.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="categories"
    )
    category_code = models.CharField("Category Code", max_length=20)
    category_name = models.CharField("Category Name", max_length=100)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "category"
        verbose_name_plural = "categories"
        ordering = ["category_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "category_code"],
                name="uniq_category_code_per_company",
                violation_error_message="A category with this code already exists.",
            ),
        ]
        indexes = [models.Index(fields=["company"])]

    def __str__(self):
        return self.category_name


class VehicleType(ApprovalMixin, TimeStampedModel):
    """A vehicle type/model, nested under a Category and tied to a Brand
    (e.g. "Monaco" under BYKY / brand Byky).

    Legacy (ImsSubcategory) had no code column at all -- only a name --
    which is why vehicle_type_code is optional here, unlike Brand/Category's
    required, unique codes. It also carried a self-reference
    (ParentSubCategoryId) that, like Category's, is left out: not evidenced
    as used.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="vehicle_types"
    )
    category = models.ForeignKey(
        "fleet.Category", on_delete=models.PROTECT, related_name="vehicle_types"
    )
    brand = models.ForeignKey(
        "fleet.Brand", on_delete=models.PROTECT, related_name="vehicle_types"
    )
    vehicle_type_code = models.CharField("Vehicle Type Code", max_length=20, blank=True)
    vehicle_type_name = models.CharField("Vehicle Type Name", max_length=100)
    description = models.TextField(blank=True)
    # Both optional -- a vehicle type can be priced before its tax rates are
    # decided. Decimal, not float, per CLAUDE.md's money/rate rule.
    tax_percentage = models.DecimalField(
        "Tax Percentage", max_digits=5, decimal_places=2, null=True, blank=True
    )
    other_tax = models.DecimalField(
        "Other Tax", max_digits=5, decimal_places=2, null=True, blank=True
    )

    class Meta:
        db_table = "vehicle_type"
        ordering = ["vehicle_type_name"]
        indexes = [
            models.Index(fields=["company"]),
            models.Index(fields=["category"]),
            models.Index(fields=["brand"]),
        ]

    def __str__(self):
        return self.vehicle_type_name


class UOM(ApprovalMixin, TimeStampedModel):
    """Unit of measure (e.g. Number). Legacy ImsUnit had a real code
    ("NO"), unlike Vehicle Type's code, so uom_code is required and unique
    per company here, same convention as Brand/Category. ImsUnit also
    carried ParentUnitId and HasSerialNo -- only 1 row ever existed in QA,
    both unused (null/0), so neither is carried forward.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="uoms"
    )
    uom_code = models.CharField("UOM Code", max_length=20)
    uom_name = models.CharField("UOM Name", max_length=100)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "uom"
        verbose_name = "UOM"
        verbose_name_plural = "UOMs"
        ordering = ["uom_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "uom_code"],
                name="uniq_uom_code_per_company",
                violation_error_message="A UOM with this code already exists.",
            ),
        ]
        indexes = [models.Index(fields=["company"])]

    def __str__(self):
        return self.uom_name


class AssetType(ApprovalMixin, TimeStampedModel):
    """A type of asset (e.g. Vehicle, Battery, Charger). No legacy table maps
    to this -- new entity, built clean. asset_type_code is optional, same
    convention as Vehicle Type's code (no legacy precedent to require it,
    unlike Brand/Category/UOM's codes)."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="asset_types"
    )
    asset_type_code = models.CharField("Asset Type Code", max_length=20, blank=True)
    asset_type_name = models.CharField("Asset Type", max_length=100)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "asset_type"
        ordering = ["asset_type_name"]
        indexes = [models.Index(fields=["company"])]

    def __str__(self):
        return self.asset_type_name


class Asset(ApprovalMixin, TimeStampedModel):
    """One physical asset (a vehicle, typically). asset_type is required;
    brand, custodian and branch are deliberately optional -- current
    location/assignment is tracked elsewhere (Inventory Branch Mapping),
    this table is the asset's own record."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="assets"
    )
    asset_code = models.CharField("Asset Code", max_length=30)
    asset_type = models.ForeignKey(
        "fleet.AssetType", on_delete=models.PROTECT, related_name="assets"
    )
    brand = models.ForeignKey(
        "fleet.Brand", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assets",
    )
    serial_no = models.CharField("Serial No", max_length=50, blank=True)
    manufacturer = models.CharField(max_length=100, blank=True)
    custodian = models.ForeignKey(
        "crew.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="custodied_assets",
    )
    supplier = models.CharField(max_length=150, blank=True)
    purchase_invoice_no = models.CharField("Purchase Invoice No", max_length=50, blank=True)
    description = models.TextField(blank=True)
    warranty_from_date = models.DateField(null=True, blank=True)
    warranty_to_date = models.DateField(null=True, blank=True)
    branch = models.ForeignKey(
        "company.Branch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assets",
    )

    class Meta:
        db_table = "asset"
        ordering = ["asset_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "asset_code"],
                name="uniq_asset_code_per_company",
                violation_error_message="An asset with this code already exists.",
            ),
        ]
        indexes = [
            models.Index(fields=["company"]),
            models.Index(fields=["asset_type"]),
            models.Index(fields=["brand"]),
            models.Index(fields=["custodian"]),
            models.Index(fields=["branch"]),
        ]

    def __str__(self):
        return self.asset_code
