"""Packages for the operator tablet: the download (POST /api/v1/operator/
packages) and the check an order line naming a package must pass.

The same contract as the fare download (apps/fare/services.py::device_fares):
the station comes from the session, packages in force on the day or the day
after are sent whole, each answer replaces what the tablet holds. Packages are
sent even when an item has no fare at the station -- the tablet decides what
it can sell (owner, 19 Oct 2026). Nothing here prices a rental.
"""

import datetime

from django.db.models import Q

from apps.fare.models import (
    Package,
    PackageLevel,
    PromotionFor,
    PromotionType,
    SlabDateMode,
    SlabDay,
)
from apps.fare.package_services import with_children
from core.enums import ApprovalStatus

# Day names -> the API's weekday numbers, Monday=0 ... Sunday=6 (as the fares).
WEEKDAY = {
    SlabDay.MONDAY: 0, SlabDay.TUESDAY: 1, SlabDay.WEDNESDAY: 2, SlabDay.THURSDAY: 3,
    SlabDay.FRIDAY: 4, SlabDay.SATURDAY: 5, SlabDay.SUNDAY: 6,
}
# Most specific scope first, as the fare download sends the station's fare first.
LEVEL_ORDER = {PackageLevel.BRANCH: 0, PackageLevel.LOCATION: 1, PackageLevel.COMPANY: 2}


def applies_to(branch):
    """Packages that cover this station: the company's, this station's own,
    or its location's."""
    return (Q(level=PackageLevel.COMPANY) | Q(level=PackageLevel.BRANCH, branch=branch)
            | Q(level=PackageLevel.LOCATION, location=branch.location_id))


def live(branch):
    """Active, approved packages of the station's company that cover it."""
    return Package.objects.filter(
        applies_to(branch), company=branch.company_id, is_active=True, approval_status=ApprovalStatus.APPROVED,
    )


def unavailable(package, branch, day):
    """Why an order line at `branch` on `day` cannot use this package, or
    None when it can."""
    if package.company_id != branch.company_id:
        return "This package is not this company's."
    if not package.is_active or package.approval_status != ApprovalStatus.APPROVED:
        return f"Package {package.package_code} is not active."
    if not live(branch).filter(pk=package.pk).exists():
        return f"Package {package.package_code} does not apply at this station."
    if not package.valid_from <= day <= package.valid_to:
        return f"Package {package.package_code} is not in force on {day:%d %b %Y}."
    return None


def device_packages(branch, day, *, vehicle_type_id=None):
    """{date, packages, by_vehicle_type}: every package covering the station
    in force on `day` or the day after, most specific scope first."""
    packages = live(branch).filter(valid_from__lte=day + datetime.timedelta(days=1), valid_to__gte=day)
    if vehicle_type_id is not None:
        packages = packages.filter(
            Q(items__vehicle_type_id=vehicle_type_id) | Q(free_items__vehicle_type_id=vehicle_type_id)
            | Q(time_slabs__vehicle_type_id=vehicle_type_id)
        ).distinct()
    rows = sorted(with_children(packages), key=lambda p: (LEVEL_ORDER[p.level], p.package_code, p.pk))

    by_vehicle_type = {}
    out = []
    for package in rows:
        out.append(_package_json(package))
        mentioned = {r.vehicle_type_id for r in package.items.all()} | \
                    {r.vehicle_type_id for r in package.free_items.all()} | \
                    {r.vehicle_type_id for r in package.time_slabs.all()}
        for vt in sorted(mentioned):
            by_vehicle_type.setdefault(str(vt), []).append(package.pk)
    return {"date": day.isoformat(), "packages": out, "by_vehicle_type": by_vehicle_type}


def _band(package, value):
    """A vehicle count is a whole number; an amount keeps its decimals."""
    return f"{value.normalize():f}" if package.promotion_for == PromotionFor.QUANTITY else str(value)


def _hhmm(value):
    return value.strftime("%H:%M")


def _package_json(package):
    quantity = package.promotion_type == PromotionType.QUANTITY
    free = list(package.free_items.all())
    return {
        "package_id": package.pk,
        "version": (package.modified_on or package.created_on).isoformat(),
        "code": package.package_code,
        "name": package.package_name,
        "level": package.level,
        "valid_from": package.valid_from.isoformat(),
        "valid_to": package.valid_to.isoformat(),
        "qualifies": {"promotion_for": package.promotion_for, "lower": _band(package, package.lower_value),
                      "upper": _band(package, package.upper_value)},
        "promotion_type": package.promotion_type,
        "items": [
            {"item_id": r.pk, "vehicle_type_id": r.vehicle_type_id, "package_minutes": r.package_minutes,
             "value": None if quantity or r.value is None else str(r.value)}
            for r in package.items.all()
        ],
        "free_items": {
            "mode": package.free_or_package_price,
            "items": [{"free_item_id": r.pk, "vehicle_type_id": r.vehicle_type_id,
                       "package_minutes": r.package_minutes, "value": str(r.value)} for r in free],
        } if quantity and package.free_item_selectable and free else None,
        "time_slabs": [
            {"slab_id": r.pk,
             "on_date": r.specific_date.isoformat() if r.date_mode == SlabDateMode.SPECIFIC_DATE else None,
             "weekday": WEEKDAY.get(r.day), "start": _hhmm(r.from_time), "end": _hhmm(r.to_time),
             "vehicle_type_id": r.vehicle_type_id, "package_minutes": r.package_minutes, "value": str(r.value)}
            for r in package.time_slabs.all()
        ] if package.time_slab_applicable else [],
        "free_item_note": package.free_item_selectable_note,
    }
