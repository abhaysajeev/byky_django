"""Fleet rules more than one caller needs. Views only translate HTTP."""

from django.utils import timezone

from apps.fleet.models import Vehicle
from core.enums import ApprovalStatus
from core.timezones import zone_for


def device_vehicles(branch, *, at=None):
    """Every vehicle at `branch` for the operator app, nested category ->
    vehicle type -> vehicles. "At" means `current_branch` -- set by hand for
    now, by Inventory Branch Mapping later; this function does not change.

    Only active, approved vehicles whose type and category are active and
    approved too. `is_available` is sent as-is so the app can say why a
    scanned vehicle cannot be rented. Categories and types with no vehicle
    here are left out.
    """
    approved = ApprovalStatus.APPROVED
    vehicles = (
        Vehicle.objects.filter(
            current_branch=branch, company=branch.company, is_active=True, approval_status=approved,
            vehicle_type__is_active=True, vehicle_type__approval_status=approved,
            vehicle_type__category__is_active=True, vehicle_type__category__approval_status=approved,
        )
        .select_related("vehicle_type__category", "vehicle_type__brand", "uom")
        .order_by("vehicle_type__category__category_name", "vehicle_type__vehicle_type_name", "vehicle_code")
    )

    categories = {}
    for vehicle in vehicles:
        vehicle_type = vehicle.vehicle_type
        category = vehicle_type.category
        entry = categories.setdefault(category.pk, {
            "category_id": category.pk, "code": category.category_code, "name": category.category_name,
            "vehicle_types": {},
        })
        type_entry = entry["vehicle_types"].setdefault(vehicle_type.pk, {
            "vehicle_type_id": vehicle_type.pk, "code": vehicle_type.vehicle_type_code,
            "name": vehicle_type.vehicle_type_name, "brand": vehicle_type.brand.brand_name,
            "tax_percentage": (str(vehicle_type.tax_percentage)
                               if vehicle_type.tax_percentage is not None else None),
            "vehicles": [],
        })
        type_entry["vehicles"].append({
            "vehicle_id": vehicle.pk, "vehicle_code": vehicle.vehicle_code,
            "vehicle_name": vehicle.vehicle_name, "rfid_epc": vehicle.rfid_epc,
            "uom": vehicle.uom.uom_name, "is_available": vehicle.is_available,
        })

    total = sum(len(t["vehicles"]) for c in categories.values() for t in c["vehicle_types"].values())
    now = at or timezone.now()
    return {
        "generated_at": now.astimezone(zone_for(branch.company)).isoformat(timespec="seconds"),
        "branch": {"id": branch.pk, "code": branch.short_code, "name": branch.name},
        "total_vehicles": total,
        "categories": [
            {**c, "vehicle_types": list(c["vehicle_types"].values())} for c in categories.values()
        ],
    }
