"""Vehicle Details and Asset Details -- the client's sheets of the same names.
Neither has a date range: they list what is there now.

Vehicle Details shows the vehicle's name where the sample had a barcode (we
keep one tag, the RFID); Asset Details shows our own asset fields (owner,
29 Oct 2026). Both are grouped by the station they are at, with how many of
each category / type that station holds.
"""

from collections import Counter

from django.db.models import Q

from apps.fleet.scoping import assets_for, vehicles_for
from apps.reports.base import COUNT, DATE, Column, Kpi, Report, Row, paged
from apps.reports.common import employee_name

NOWHERE = "Not at a station"


def _status(item):
    """Active or Inactive -- as the Inventory screens show it (is_active)."""
    return "Active" if item.is_active else "Inactive"


def _place(qs, f, branch):
    if f.get("state"):
        qs = qs.filter(**{f"{branch}__location__state_id": f.get("state")})
    if f.get("branch"):
        qs = qs.filter(**{f"{branch}_id": f.get("branch")})
    return qs


class VehicleDetails(Report):
    slug = "vehicle-details"
    title = "Vehicle Details"
    sheet_title = "Vehicle Details Report"
    page_code = "reports.vehicle_details"
    filters = ("state", "branch", "category", "vehicle_type", "vehicle_no", "rfid")
    unit = ("vehicle", "vehicles")
    dated = False
    empty = "No vehicle matches these filters."
    description = "Every vehicle by station, category and type, with its RFID tag."

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        vehicles = _place(vehicles_for(ctx.user), f, "current_branch")
        if f.get("category"):
            vehicles = vehicles.filter(vehicle_type__category_id=f.get("category"))
        if f.get("vehicle_type"):
            vehicles = vehicles.filter(vehicle_type_id=f.get("vehicle_type"))
        if f.get("vehicle_no"):
            text = f.get("vehicle_no")
            vehicles = vehicles.filter(Q(vehicle_code__icontains=text) | Q(vehicle_name__icontains=text)
                                       | Q(identifier__icontains=text))
        if f.get("rfid"):
            vehicles = vehicles.filter(rfid_epc__icontains=f.get("rfid"))
        vehicles = list(vehicles.select_related("current_branch", "vehicle_type__category", "vehicle_type__brand"))

        def station(v):
            return v.current_branch.name if v.current_branch else NOWHERE

        categories = Counter((station(v), v.vehicle_type.category_id) for v in vehicles)
        types = Counter((station(v), v.vehicle_type_id) for v in vehicles)
        vehicles.sort(key=lambda v: (station(v) == NOWHERE, station(v).lower(),
                                     v.vehicle_type.category.category_name.lower(),
                                     v.vehicle_type.vehicle_type_name.lower(), v.vehicle_code.lower()))
        columns = [
            Column("branch", "Branch", width=24),
            Column("category", "Category", width=22),
            Column("category_count", "Category Count", COUNT, summed=False, width=10),
            Column("type", "Vehicle Type", width=20),
            Column("type_count", "Vehicle Type Count", COUNT, summed=False, width=10),
            Column("brand", "Brand", width=14),
            Column("code", "Vehicle Code", width=14),
            Column("name", "Vehicle Name", width=16),
            Column("rfid", "RFID Tag No", width=18),
            Column("status", "Status", width=16),
        ]
        rows = [Row({
            "branch": station(v), "category": v.vehicle_type.category.category_name,
            "category_count": categories[(station(v), v.vehicle_type.category_id)],
            "type": v.vehicle_type.vehicle_type_name, "type_count": types[(station(v), v.vehicle_type_id)],
            "brand": v.vehicle_type.brand.brand_name, "code": v.vehicle_code, "name": v.vehicle_name,
            "rfid": v.rfid_epc or None, "status": _status(v),
        }) for v in vehicles]
        return paged(rows, page, per_page, columns=columns, total=None, count=len(rows),
                     kpis=[Kpi("Vehicles", len(rows), COUNT),
                           Kpi("Stations", len({r.cells["branch"] for r in rows} - {NOWHERE}), COUNT),
                           Kpi("Categories", len({v.vehicle_type.category_id for v in vehicles}), COUNT),
                           Kpi("Vehicle types", len({v.vehicle_type_id for v in vehicles}), COUNT)])


class AssetDetails(Report):
    slug = "asset-details"
    title = "Asset Details"
    sheet_title = "Asset Details Report"
    page_code = "reports.asset_details"
    filters = ("state", "branch", "asset_type", "asset")
    unit = ("asset", "assets")
    dated = False
    empty = "No asset matches these filters."
    description = "Every asset by station and type, with who holds it."

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        assets = _place(assets_for(ctx.user), f, "branch")
        if f.get("asset_type"):
            assets = assets.filter(asset_type_id=f.get("asset_type"))
        if f.get("asset"):
            text = f.get("asset")
            assets = assets.filter(Q(asset_code__icontains=text) | Q(serial_no__icontains=text))
        assets = list(assets.select_related("branch", "asset_type", "brand", "custodian"))

        def station(a):
            return a.branch.name if a.branch else NOWHERE

        types = Counter((station(a), a.asset_type_id) for a in assets)
        assets.sort(key=lambda a: (station(a) == NOWHERE, station(a).lower(), a.asset_type.asset_type_name.lower(),
                                   a.asset_code.lower()))
        columns = [
            Column("branch", "Branch", width=24),
            Column("type", "Asset Type", width=20),
            Column("type_count", "Asset Type Count", COUNT, summed=False, width=10),
            Column("brand", "Brand", width=14),
            Column("code", "Asset Code", width=16),
            Column("serial", "Serial No", width=18),
            Column("manufacturer", "Manufacturer", width=18),
            Column("custodian", "Custodian", width=26),
            Column("warranty", "Warranty To", DATE, width=12),
            Column("status", "Status", width=16),
        ]
        rows = [Row({
            "branch": station(a), "type": a.asset_type.asset_type_name,
            "type_count": types[(station(a), a.asset_type_id)], "brand": a.brand.brand_name if a.brand else None,
            "code": a.asset_code, "serial": a.serial_no or None, "manufacturer": a.manufacturer or None,
            "custodian": employee_name(a.custodian), "warranty": a.warranty_to_date, "status": _status(a),
        }) for a in assets]
        return paged(rows, page, per_page, columns=columns, total=None, count=len(rows),
                     kpis=[Kpi("Assets", len(rows), COUNT),
                           Kpi("Stations", len({r.cells["branch"] for r in rows} - {NOWHERE}), COUNT),
                           Kpi("Asset types", len({a.asset_type_id for a in assets}), COUNT)])
