"""Fleet rules more than one caller needs. Views only translate HTTP."""

import re

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.fleet.models import IDENTIFIER_SEQUENCE, Category, Vehicle, VehicleType
from core.enums import ApprovalStatus
from core.timezones import zone_for


class UnknownCategory(Exception):
    """Not this branch's company's, or not active and approved."""


class UnknownVehicleType(Exception):
    """Not this branch's company's, or it, or its category, is not active and approved."""


def device_vehicles(branch, *, vehicle_category_id=None, vehicle_type_id=None, at=None):
    """Every vehicle at `branch` for the operator app, nested category ->
    vehicle type -> vehicles. "At" means `current_branch` -- set by hand for
    now, by Inventory Branch Mapping later; this function does not change.

    Only active, approved vehicles whose type and category are active and
    approved too. `is_available` is sent as-is so the app can say why a
    scanned vehicle cannot be rented. Categories and types with no vehicle
    here are left out.

    `vehicle_category_id` and `vehicle_type_id` only narrow the answer, the
    same way as the fares download (apps/fare/services.py::device_fares): one
    this branch cannot use raises UnknownCategory / UnknownVehicleType; a
    usable one with no vehicle here -- or a type outside the given category --
    gives an empty list.
    """
    approved = ApprovalStatus.APPROVED
    usable = {"company": branch.company, "is_active": True, "approval_status": approved}
    if vehicle_category_id is not None and not Category.objects.filter(pk=vehicle_category_id, **usable).exists():
        raise UnknownCategory()
    if vehicle_type_id is not None and not VehicleType.objects.filter(
        pk=vehicle_type_id, category__is_active=True, category__approval_status=approved, **usable,
    ).exists():
        raise UnknownVehicleType()

    vehicles = Vehicle.objects.filter(
        current_branch=branch, company=branch.company, is_active=True, approval_status=approved,
        vehicle_type__is_active=True, vehicle_type__approval_status=approved,
        vehicle_type__category__is_active=True, vehicle_type__category__approval_status=approved,
    )
    if vehicle_category_id is not None:
        vehicles = vehicles.filter(vehicle_type__category_id=vehicle_category_id)
    if vehicle_type_id is not None:
        vehicles = vehicles.filter(vehicle_type_id=vehicle_type_id)
    vehicles = vehicles.select_related("vehicle_type__category", "vehicle_type__brand", "uom").order_by(
        "vehicle_type__category__category_name", "vehicle_type__vehicle_type_name", "identifier_no",
    )

    categories = {}
    for vehicle in vehicles:
        vehicle_type = vehicle.vehicle_type
        category = vehicle_type.category
        entry = categories.setdefault(category.pk, {
            "category_id": category.pk, "code": category.category_code, "name": category.category_name,
            "vehicle_count": 0, "vehicle_types": {},
        })
        type_entry = entry["vehicle_types"].setdefault(vehicle_type.pk, {
            "vehicle_type_id": vehicle_type.pk, "code": vehicle_type.vehicle_type_code,
            "name": vehicle_type.vehicle_type_name, "brand": vehicle_type.brand.brand_name,
            "tax_percentage": (str(vehicle_type.tax_percentage)
                               if vehicle_type.tax_percentage is not None else None),
            "vehicle_count": 0, "vehicles": [],
        })
        type_entry["vehicles"].append({
            "vehicle_id": vehicle.pk, "vehicle_identifier": vehicle.identifier, "vehicle_code": vehicle.vehicle_code,
            "vehicle_name": vehicle.vehicle_name, "rfid_epc": vehicle.rfid_epc,
            "uom": vehicle.uom.uom_name, "is_available": vehicle.is_available,
        })
        type_entry["vehicle_count"] += 1
        entry["vehicle_count"] += 1

    now = at or timezone.now()
    return {
        "generated_at": now.astimezone(zone_for(branch.company)).isoformat(timespec="seconds"),
        "branch": {"id": branch.pk, "code": branch.short_code, "name": branch.name},
        "filters": {"vehicle_category_id": vehicle_category_id, "vehicle_type_id": vehicle_type_id},
        "total_vehicles": sum(c["vehicle_count"] for c in categories.values()),
        "categories": [
            {**c, "vehicle_types": list(c["vehicle_types"].values())} for c in categories.values()
        ],
    }


def natural(text):
    """'MO 10' -> ['mo ', 10, ''] so numbers inside names sort as numbers."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text or "")]


def renumber_identifiers():
    """Give every vehicle a gap-free identifier, VB0001 upwards, in the order
    migration 0011 first numbered them: category, vehicle type, vehicle name
    (number-aware, so "MO 2" before "MO 10"), then code and id. The sequence
    carries on from the last number. Returns how many were numbered.

    Identifiers are meant never to change; this is for cleaning the fleet up
    before anything outside the system (a tablet, a printed label) uses them.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            # No insert may take a number from the sequence while it is reset.
            cursor.execute(f"LOCK TABLE {Vehicle._meta.db_table} IN EXCLUSIVE MODE")
        vehicles = list(Vehicle.objects.select_related("vehicle_type__category").only(
            "pk", "identifier_no", "vehicle_code", "vehicle_name",
            "vehicle_type__vehicle_type_name", "vehicle_type__category__category_name",
        ))
        ordered = sorted(vehicles, key=lambda v: (
            natural(v.vehicle_type.category.category_name), natural(v.vehicle_type.vehicle_type_name),
            natural(v.vehicle_name), natural(v.vehicle_code), v.pk,
        ))
        # Out of the way first: the unique index is checked row by row.
        Vehicle.objects.update(identifier_no=F("identifier_no") * -1)
        for number, vehicle in enumerate(ordered, start=1):
            vehicle.identifier_no = number
        Vehicle.objects.bulk_update(ordered, ["identifier_no"], batch_size=1000)
        with connection.cursor() as cursor:
            # is_called=false with 1 on an empty table: the first vehicle gets 1.
            cursor.execute("SELECT setval(%s, %s, %s)", [IDENTIFIER_SEQUENCE, max(len(ordered), 1), bool(ordered)])
    return len(ordered)
