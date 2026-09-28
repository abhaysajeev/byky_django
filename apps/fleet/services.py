"""Fleet rules more than one caller needs. Views only translate HTTP."""

from django.utils import timezone

from apps.fleet.models import Category, Vehicle, VehicleType
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
