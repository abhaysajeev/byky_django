"""Vehicle Management.

SRS module 4, built from the legacy ERP.IMS area -- but only the vehicle
subset of it. IMS is a fork of an FMCG distribution product (CLAUDE.md
section 3), so most of its tables (ImsBatch, ImsFeatures, ImsStockAvailability,
...) are out of scope here; this app carries forward only what a vehicle rental
fleet actually needs.
"""

from django.db import models
from django.db.models import Case, Func, Value, When
from django.db.models.functions import Cast, Concat, LPad

from apps.rental.models import DecisionChannel
from core.models import ApprovalMixin, TimeStampedModel

# Vehicle.identifier: "VB" and a four-digit running number from this
# sequence (migration 0011), longer once it passes 9999 (VB10000).
IDENTIFIER_SEQUENCE = "vehicle_identifier_seq"
IDENTIFIER_PREFIX = "VB"
IDENTIFIER_DIGITS = 4


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
    # Direct rent: a fixed-time ride (the small kids' vehicles in an enclosed
    # area) billed in full before handover -- no runtime fare, the vehicle is
    # never tracked as out on rent. The app reads it from the vehicles
    # download and bills accordingly.
    #
    # Legacy: ImsSubcategory.IsReturn, inverted (IsReturn=0 = direct), sent to
    # the devices by SFA_GetAllcategoriesSubCategoriesAndStockItems. In the QA
    # data exactly one type has IsReturn=0 -- DRIFT CAR -- and all 3,424 of its
    # bill lines are DMSOrder.IsDirectBill=1; no other type has a direct bill.
    # (IsCustomerNeeded, 0 for the same type, is not carried: not needed yet.)
    is_direct_rent = models.BooleanField("Direct Rent", default=False)

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


class Vehicle(ApprovalMixin, TimeStampedModel):
    """One physical rental vehicle. Legacy ImsStockItem, stripped of its
    FMCG-lineage columns (MRP, ReorderLevel, HSNCode, ...) and of fields
    verified unused (ParentStockID, IsFragile, LogoName, StockImage --
    "populated" by a null-check but every sample was an empty string).

    No category/brand fields here -- both come from vehicle_type, so a
    vehicle can never disagree with its own type. Distinct from Asset:
    Vehicle is the rental catalog entry, Asset is a separate equipment
    registry -- confirmed with the user, not the same thing.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="vehicles"
    )
    # The internal identifier: never typed, never edited, never reused. The
    # number comes from a Postgres sequence on every insert -- a screen save,
    # create(), or an import's bulk_create alike -- the same way as
    # Device.device_registration_id. `identifier` is the text shown and
    # searched ("VB0042"), computed by the database from the number.
    identifier_no = models.BigIntegerField(
        unique=True, editable=False,
        db_default=Func(Value(IDENTIFIER_SEQUENCE), function="nextval"),
    )
    identifier = models.GeneratedField(
        verbose_name="Vehicle Identifier",
        expression=Concat(
            Value(IDENTIFIER_PREFIX),
            Case(
                When(identifier_no__lt=10 ** IDENTIFIER_DIGITS,
                     then=LPad(Cast("identifier_no", models.CharField()), IDENTIFIER_DIGITS, Value("0"))),
                default=Cast("identifier_no", models.CharField()),     # past 9999: grows, never truncates
            ),
        ),
        output_field=models.CharField(max_length=30),
        db_persist=True,
        unique=True,
    )
    vehicle_code = models.CharField("Vehicle Code", max_length=30)
    vehicle_name = models.CharField("Vehicle Name", max_length=100)
    vehicle_type = models.ForeignKey(
        "fleet.VehicleType", on_delete=models.PROTECT, related_name="vehicles"
    )
    uom = models.ForeignKey(
        "fleet.UOM", on_delete=models.PROTECT, related_name="vehicles"
    )
    # Optional: real legacy data (imsstockitem, live export) has vehicles
    # whose BarCode is a placeholder shared across several rows ("1", "2",
    # "4", ...) rather than a real per-vehicle tag -- a true value still
    # needs to be unique, but "no tag assigned yet" has to be representable.
    rfid_epc = models.CharField("RFID Tag EPC", max_length=64, blank=True)
    is_available = models.BooleanField(default=True)
    # Where the vehicle is now (design/vehicle_inventory_schema: "set by
    # mapping and transfers"). Set by hand on the Vehicle screen until
    # Inventory Branch Mapping exists; that will then keep it current. Empty
    # means not at any station (warehouse, in transit, not yet placed).
    # Always one of the vehicle's own company's branches -- a composite FK in
    # migration 0010 refuses anything else.
    current_branch = models.ForeignKey(
        "company.Branch", null=True, blank=True, on_delete=models.PROTECT,
        related_name="vehicles", verbose_name="Current Branch",
    )

    class Meta:
        db_table = "vehicle"
        ordering = ["identifier_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "vehicle_code"],
                name="uniq_vehicle_code_per_company",
                violation_error_message="A vehicle with this code already exists.",
            ),
            models.UniqueConstraint(
                fields=["company", "rfid_epc"],
                condition=~models.Q(rfid_epc=""),
                name="uniq_vehicle_rfid_epc_per_company",
                violation_error_message="A vehicle with this RFID tag already exists.",
            ),
        ]
        indexes = [
            models.Index(fields=["company"]),
            models.Index(fields=["vehicle_type"]),
            models.Index(fields=["uom"]),
        ]

    def __str__(self):
        return self.vehicle_code


# --- Service & Maintenance ----------------------------------------------------------
#
# New in the rebuild: legacy had no maintenance tables (the AMS/EMS areas were
# switched off). Built from the 2026 manager-app spec §5 and the owner's
# decisions of 23-26 Oct 2026 (design/inventory/service-and-image-sharing.md):
# the operator reports a vehicle or a station asset; a reported vehicle is out of
# renting from that moment; a manager approves or rejects; the web puts it back
# in service.

class ServiceRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Under maintenance"
    REJECTED = "rejected", "Rejected"
    COMPLETED = "completed", "Completed"


# A request still holding its vehicle or asset: the vehicle cannot be rented.
SERVICE_OPEN = (ServiceRequestStatus.PENDING, ServiceRequestStatus.APPROVED)


class ServiceRequest(TimeStampedModel):
    # The tablet's sync_id: a resent report finds its own row.
    id = models.UUIDField(primary_key=True, editable=False)
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="service_requests")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="service_requests")
    # Counted per station (ServiceRequestCounter); request_no is written once --
    # the station's short code may change later, the number already given may not.
    number = models.PositiveIntegerField()
    request_no = models.CharField(max_length=30)

    vehicle = models.ForeignKey(
        "fleet.Vehicle", null=True, blank=True, on_delete=models.PROTECT, related_name="service_requests",
    )
    asset = models.ForeignKey(
        "fleet.Asset", null=True, blank=True, on_delete=models.PROTECT, related_name="service_requests",
    )
    issue = models.TextField()
    photo = models.ForeignKey("photos.Photo", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    device = models.ForeignKey("devices.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField()           # tablet time

    status = models.CharField(max_length=10, choices=ServiceRequestStatus.choices,
                              default=ServiceRequestStatus.PENDING)
    decided_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_channel = models.CharField(max_length=10, choices=DecisionChannel.choices, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    # Back in service -- on the web only (owner, 26 Oct 2026).
    completed_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)
    completion_note = models.TextField(blank=True)

    class Meta:
        db_table = "service_request"
        ordering = ["-requested_at"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(vehicle__isnull=False, asset__isnull=True)
                           | models.Q(vehicle__isnull=True, asset__isnull=False)),
                name="service_request_one_target",
            ),
            models.UniqueConstraint(
                fields=["vehicle"], condition=models.Q(status__in=SERVICE_OPEN, vehicle__isnull=False),
                name="uniq_open_service_request_per_vehicle",
            ),
            models.UniqueConstraint(
                fields=["asset"], condition=models.Q(status__in=SERVICE_OPEN, asset__isnull=False),
                name="uniq_open_service_request_per_asset",
            ),
            models.UniqueConstraint(fields=["branch", "number"], name="uniq_service_request_number_per_branch"),
            models.CheckConstraint(
                condition=(
                    models.Q(status=ServiceRequestStatus.PENDING, decided_at__isnull=True, completed_at__isnull=True)
                    | models.Q(status__in=(ServiceRequestStatus.APPROVED, ServiceRequestStatus.REJECTED),
                               decided_at__isnull=False, completed_at__isnull=True)
                    | models.Q(status=ServiceRequestStatus.COMPLETED, decided_at__isnull=False,
                               completed_at__isnull=False)
                ),
                name="service_request_status_times",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status", "requested_at"]),
            models.Index(fields=["branch", "requested_at"]),
        ]

    def __str__(self):
        return self.request_no

    @property
    def is_open(self):
        return self.status in SERVICE_OPEN


class ServiceRequestCounter(models.Model):
    """The last service-request number given at a station. Taken under a row
    lock inside the report's transaction, so two reports never share a number
    (a Max()+1 would race; a global sequence would not count per station)."""

    branch = models.OneToOneField("company.Branch", primary_key=True, on_delete=models.CASCADE,
                                  related_name="+")
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "service_request_counter"

    def __str__(self):
        return f"{self.branch_id}: {self.last_number}"


# --- Image Sharing ------------------------------------------------------------------
#
# Legacy "MarketingIntelligence" (SfaMarketingIntelligence: a base64 photo and
# remarks from the tablet, approved on the web). Rebuilt simpler (owner, 26 Oct
# 2026): photo + remarks, viewed and deleted on the web, viewed in the manager
# app -- no approval, nothing depends on one.

class ImageShare(TimeStampedModel):
    id = models.UUIDField(primary_key=True, editable=False)    # the tablet's sync_id
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="image_shares")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="image_shares")
    device = models.ForeignKey("devices.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    shared_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    shared_at = models.DateTimeField()              # tablet time
    remarks = models.TextField()
    photo = models.ForeignKey("photos.Photo", on_delete=models.PROTECT, related_name="+")
    # Deleted on the web: kept, out of every list, its photo no longer served.
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "image_share"
        ordering = ["-shared_at"]
        indexes = [models.Index(fields=["company", "shared_at"]), models.Index(fields=["branch", "shared_at"])]

    def __str__(self):
        return f"Image shared at {self.branch_id} on {self.shared_at:%Y-%m-%d %H:%M}"
