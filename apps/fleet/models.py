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
