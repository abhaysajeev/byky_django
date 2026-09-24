"""One-time import of Category/VehicleType/Vehicle from a live legacy data
export (ImsCategory, ImsSubcategory, imsstockitem sheets, exported to CSV).

Not idempotent by design -- re-running against a database that already has
this data will hit the unique constraints and stop; that is the safety net,
not a bug to fix. Meant to be run once per company, reviewed, done.

Rules applied, decided with the user before running:
  - category_code / vehicle_type_code: legacy ImsCategory/ImsSubcategory never
    had a code column at all (see apps/fleet/models.py docstrings). Category's
    code is required, so one is derived from the name (slugified, deduped on
    collision). VehicleType's code is optional, so it is left blank -- same
    "no legacy precedent" reasoning already used when that field was designed.
  - brand: every VehicleType gets the single existing "BERG" brand. Not
    evidenced per vehicle type -- an explicit, deliberate placeholder until
    real brand data exists, not a migrated fact.
  - vehicle_code duplicates (legacy StockCode, ~16 pairs in the live data):
    kept in first-seen order, every row after the first gets the code
    suffixed "-2", "-3", ... so nothing is silently dropped.
  - rfid_epc duplicates (legacy BarCode): a value that appears on more than
    one row is not a real per-vehicle tag (observed as placeholders like "1",
    "2", "4" shared across a batch) -- every row sharing a duplicated value
    imports with rfid_epc blank, not arbitrarily assigned to one of them.
  - uom: legacy UnitID is always 1 ("Number") across every row in the
    export -- one UOM row, get_or_create'd, used for every vehicle.
"""

import csv
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.company.models import Company
from apps.fleet.models import UOM, Brand, Category, Vehicle, VehicleType
from core.enums import ApprovalStatus


def _slug_code(name, max_len=20):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").upper()
    return slug[:max_len] or "CAT"


class Command(BaseCommand):
    help = (
        "Import Category, VehicleType and Vehicle rows from a live legacy "
        "data export (category.csv / subcategory.csv / stockitem.csv, from "
        "the ImsCategory / ImsSubcategory / imsstockitem sheets)."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_dir", help="Directory holding category.csv, subcategory.csv, stockitem.csv")
        parser.add_argument("--company", required=True, help="Company short_code to import into")
        parser.add_argument("--brand", default="BERG", help="Brand name to link to every Vehicle Type")

    def handle(self, *args, **options):
        csv_dir = Path(options["csv_dir"])
        company = Company.objects.filter(short_code=options["company"]).first()
        if company is None:
            raise CommandError(f"No company with short_code={options['company']!r}.")
        brand = Brand.objects.filter(company=company, brand_name=options["brand"]).first()
        if brand is None:
            raise CommandError(
                f"No brand named {options['brand']!r} for {company.short_code} -- "
                "create it first (this command does not invent brand data)."
            )

        categories = list(csv.DictReader(open(csv_dir / "category.csv")))
        subcategories = list(csv.DictReader(open(csv_dir / "subcategory.csv")))
        items = list(csv.DictReader(open(csv_dir / "stockitem.csv")))

        with transaction.atomic():
            uom, _ = UOM.objects.get_or_create(
                company=company, uom_code="NO",
                defaults={"uom_name": "Number", "approval_status": ApprovalStatus.APPROVED},
            )

            category_map = {}  # legacy CategoryID -> Category
            used_codes = set()
            for row in categories:
                code = _slug_code(row["CategoryName"])
                base, n = code, 2
                while code in used_codes:
                    code = f"{base[:17]}-{n}"
                    n += 1
                used_codes.add(code)
                obj = Category.objects.create(
                    company=company,
                    category_code=code,
                    category_name=row["CategoryName"].strip(),
                    description=(row.get("Description") or "").strip(),
                    is_active=row["IsActive"] == "1",
                    approval_status=(
                        ApprovalStatus.APPROVED if row["IsApproved"] == "1" else ApprovalStatus.PENDING
                    ),
                )
                category_map[row["CategoryID"]] = obj

            vehicle_type_map = {}  # legacy SubCategoryID -> VehicleType
            for row in subcategories:
                category = category_map.get(row["CategoryID"])
                if category is None:
                    self.stderr.write(
                        f"SubCategoryID {row['SubCategoryID']}: unknown CategoryID "
                        f"{row['CategoryID']!r}, skipped."
                    )
                    continue
                obj = VehicleType.objects.create(
                    company=company,
                    category=category,
                    brand=brand,
                    vehicle_type_code="",
                    vehicle_type_name=row["SubCategoryName"].strip(),
                    description=(row.get("Description") or "").strip(),
                    is_active=row["IsActive"] == "1",
                    approval_status=(
                        ApprovalStatus.APPROVED if row["IsApproved"] == "1" else ApprovalStatus.PENDING
                    ),
                )
                vehicle_type_map[row["SubCategoryID"]] = obj

            # Duplicate BarCodes are placeholders, not real per-vehicle tags
            # (see module docstring) -- every row sharing one imports blank.
            barcode_counts = {}
            for row in items:
                bc = (row.get("BarCode") or "").strip()
                if bc:
                    barcode_counts[bc] = barcode_counts.get(bc, 0) + 1

            used_vehicle_codes = set()
            created = skipped = 0
            batch = []
            for row in items:
                vehicle_type = vehicle_type_map.get(row["SubCategoryID"])
                if vehicle_type is None:
                    self.stderr.write(
                        f"StockID {row['StockID']}: unknown SubCategoryID "
                        f"{row['SubCategoryID']!r}, skipped."
                    )
                    skipped += 1
                    continue

                code = row["StockCode"].strip()
                base, n = code, 2
                while code in used_vehicle_codes:
                    code = f"{base}-{n}"
                    n += 1
                used_vehicle_codes.add(code)

                bc = (row.get("BarCode") or "").strip()
                rfid_epc = bc if bc and barcode_counts.get(bc, 0) == 1 else ""

                batch.append(Vehicle(
                    company=company,
                    vehicle_code=code,
                    vehicle_name=row["StockName"].strip(),
                    vehicle_type=vehicle_type,
                    uom=uom,
                    rfid_epc=rfid_epc,
                    is_available=row.get("TemporaryActiveStatus") == "1",
                    is_active=row["IsActive"] == "1",
                    approval_status=(
                        ApprovalStatus.APPROVED if row["IsApproved"] == "1" else ApprovalStatus.PENDING
                    ),
                ))
                created += 1
                if len(batch) >= 500:
                    Vehicle.objects.bulk_create(batch)
                    batch = []
            if batch:
                Vehicle.objects.bulk_create(batch)

        self.stdout.write(self.style.SUCCESS(
            f"{len(category_map)} categories, {len(vehicle_type_map)} vehicle types, "
            f"{created} vehicles created ({skipped} stock items skipped -- unresolvable subcategory)."
        ))
