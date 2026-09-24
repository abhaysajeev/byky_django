"""Vehicle Management screens.

Follows the same shape as apps/company's screens: a shared *ScreenView base
per module, a generic drawer resolved in render_to_response, and the write
endpoints reused straight from apps.company.writes rather than redefined here
-- Brand needs nothing that generic save/delete doesn't already do.
"""

from django.urls import reverse

from apps.company import writes as company_writes
from apps.company.scoping import branches_for, companies_for
from apps.crew.scoping import employees_for
from apps.fleet import drawers, forms, scoping
from apps.fleet.models import UOM, Asset, AssetType, Brand, Category, Vehicle, VehicleType
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from theme import drawers as theme_drawers
from theme.views import ThemedTemplateView

ACTIONS = ("create", "read", "update", "delete", "print")


class FleetScreenView(PagePermissionMixin, ThemedTemplateView):
    """Shared by every screen in this module: the reference lists its drawers
    resolve against, and the permission flags its buttons check."""

    drawer_specs = drawers.SPECS

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update({
            "companies_list": list(companies_for(user).filter(is_active=True).values("id", "name")),
            "categories_list": list(
                scoping.categories_for(user).filter(is_active=True).values("id", "category_name")
            ),
            "brands_list": list(
                scoping.brands_for(user).filter(is_active=True).values("id", "brand_name")
            ),
            "asset_types_list": list(
                scoping.asset_types_for(user).filter(is_active=True).values("id", "asset_type_name")
            ),
            "employees_list": [
                {"id": e.pk, "name": f"{e.employee_code} — {e.full_name}"}
                for e in employees_for(user).filter(is_active=True)
            ],
            "branches_list": list(branches_for(user).filter(is_active=True).values("id", "name")),
            "vehicle_types_list": list(
                scoping.vehicle_types_for(user).filter(is_active=True)
                .values("id", "vehicle_type_name")
            ),
            "uoms_list": list(
                scoping.uoms_for(user).filter(is_active=True).values("id", "uom_name")
            ),
            "perm": {action: has_permission(user, self.page_code, action) for action in ACTIONS},
        })
        return context

    def render_to_response(self, context, **response_kwargs):
        # Resolved here, not in get_context_data, so a subclass's own lists are
        # already in context when a drawer's dropdowns are filled.
        specs = theme_drawers.all_for_user(
            self.drawer_specs, self.request.user.sees_every_company
        )
        context.update(theme_drawers.resolve_all(specs, context))
        return super().render_to_response(context, **response_kwargs)


class BrandListView(FleetScreenView):
    template_name = "fleet/brand_list.html"
    page_code = "fleet.brand"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        for i, brand in enumerate(scoping.brands_for(self.request.user)):
            rows.append({
                "code": brand.brand_code,
                "name": brand.brand_name,
                "manufacturer": brand.manufacturer,
                "website": brand.website_link,
                "active": brand.is_active,
                "pk": brand.pk,
                "json_id": f"scr-record-brand-{i}",
                "fields_json": {
                    "pk": brand.pk,
                    "company": brand.company_id,
                    "brand_code": brand.brand_code,
                    "brand_name": brand.brand_name,
                    "description": brand.description,
                    "manufacturer": brand.manufacturer,
                    "website_link": brand.website_link,
                    "is_active": brand.is_active,
                },
            })
        context["brands"] = rows
        context["save_url_brand"] = reverse("fleet-brand-save")
        context["delete_url_brand"] = reverse("fleet-brand-delete", args=[0])
        context["noun_brand"] = "Brand"
        return context


class BrandSave(company_writes.EntitySaveView):
    model = Brand
    form_class = forms.BrandForm
    page_code = "fleet.brand"
    noun = "Brand"


class BrandDelete(company_writes.EntityDeleteView):
    model = Brand
    page_code = "fleet.brand"
    noun = "Brand"


class CategoryListView(FleetScreenView):
    template_name = "fleet/category_list.html"
    page_code = "fleet.category"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        for i, category in enumerate(scoping.categories_for(self.request.user)):
            rows.append({
                "code": category.category_code,
                "name": category.category_name,
                "active": category.is_active,
                "pk": category.pk,
                "json_id": f"scr-record-category-{i}",
                "fields_json": {
                    "pk": category.pk,
                    "company": category.company_id,
                    "category_code": category.category_code,
                    "category_name": category.category_name,
                    "description": category.description,
                    "is_active": category.is_active,
                },
            })
        context["categories"] = rows
        context["save_url_category"] = reverse("fleet-category-save")
        context["delete_url_category"] = reverse("fleet-category-delete", args=[0])
        context["noun_category"] = "Category"
        return context


class CategorySave(company_writes.EntitySaveView):
    model = Category
    form_class = forms.CategoryForm
    page_code = "fleet.category"
    noun = "Category"


class CategoryDelete(company_writes.EntityDeleteView):
    model = Category
    page_code = "fleet.category"
    noun = "Category"


class VehicleTypeListView(FleetScreenView):
    template_name = "fleet/vehicle_type_list.html"
    page_code = "fleet.vehicle_type"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        vehicle_types = (
            scoping.vehicle_types_for(self.request.user).select_related("category", "brand")
        )
        for i, vehicle_type in enumerate(vehicle_types):
            rows.append({
                "code": vehicle_type.vehicle_type_code,
                "name": vehicle_type.vehicle_type_name,
                "category": vehicle_type.category.category_name,
                "brand": vehicle_type.brand.brand_name,
                "active": vehicle_type.is_active,
                "pk": vehicle_type.pk,
                "json_id": f"scr-record-vehicle-type-{i}",
                "fields_json": {
                    "pk": vehicle_type.pk,
                    "company": vehicle_type.company_id,
                    "category": vehicle_type.category_id,
                    "brand": vehicle_type.brand_id,
                    "vehicle_type_code": vehicle_type.vehicle_type_code,
                    "vehicle_type_name": vehicle_type.vehicle_type_name,
                    "description": vehicle_type.description,
                    "tax_percentage": (
                        str(vehicle_type.tax_percentage) if vehicle_type.tax_percentage is not None else ""
                    ),
                    "other_tax": (
                        str(vehicle_type.other_tax) if vehicle_type.other_tax is not None else ""
                    ),
                    "is_active": vehicle_type.is_active,
                },
            })
        context["vehicle_types"] = rows
        context["save_url_vehicle_type"] = reverse("fleet-vehicle-type-save")
        context["delete_url_vehicle_type"] = reverse("fleet-vehicle-type-delete", args=[0])
        context["noun_vehicle_type"] = "Vehicle Type"
        return context


class VehicleTypeSave(company_writes.EntitySaveView):
    model = VehicleType
    form_class = forms.VehicleTypeForm
    page_code = "fleet.vehicle_type"
    noun = "Vehicle Type"


class VehicleTypeDelete(company_writes.EntityDeleteView):
    model = VehicleType
    page_code = "fleet.vehicle_type"
    noun = "Vehicle Type"


class UOMListView(FleetScreenView):
    template_name = "fleet/uom_list.html"
    page_code = "fleet.uom"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        for i, uom in enumerate(scoping.uoms_for(self.request.user)):
            rows.append({
                "code": uom.uom_code,
                "name": uom.uom_name,
                "active": uom.is_active,
                "pk": uom.pk,
                "json_id": f"scr-record-uom-{i}",
                "fields_json": {
                    "pk": uom.pk,
                    "company": uom.company_id,
                    "uom_code": uom.uom_code,
                    "uom_name": uom.uom_name,
                    "description": uom.description,
                    "is_active": uom.is_active,
                },
            })
        context["uoms"] = rows
        context["save_url_uom"] = reverse("fleet-uom-save")
        context["delete_url_uom"] = reverse("fleet-uom-delete", args=[0])
        context["noun_uom"] = "UOM"
        return context


class UOMSave(company_writes.EntitySaveView):
    model = UOM
    form_class = forms.UOMForm
    page_code = "fleet.uom"
    noun = "UOM"


class UOMDelete(company_writes.EntityDeleteView):
    model = UOM
    page_code = "fleet.uom"
    noun = "UOM"


class AssetTypeListView(FleetScreenView):
    template_name = "fleet/asset_type_list.html"
    page_code = "fleet.asset_type"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        for i, asset_type in enumerate(scoping.asset_types_for(self.request.user)):
            rows.append({
                "code": asset_type.asset_type_code,
                "name": asset_type.asset_type_name,
                "active": asset_type.is_active,
                "pk": asset_type.pk,
                "json_id": f"scr-record-asset-type-{i}",
                "fields_json": {
                    "pk": asset_type.pk,
                    "company": asset_type.company_id,
                    "asset_type_code": asset_type.asset_type_code,
                    "asset_type_name": asset_type.asset_type_name,
                    "description": asset_type.description,
                    "is_active": asset_type.is_active,
                },
            })
        context["asset_types"] = rows
        context["save_url_asset_type"] = reverse("fleet-asset-type-save")
        context["delete_url_asset_type"] = reverse("fleet-asset-type-delete", args=[0])
        context["noun_asset_type"] = "Asset Type"
        return context


class AssetTypeSave(company_writes.EntitySaveView):
    model = AssetType
    form_class = forms.AssetTypeForm
    page_code = "fleet.asset_type"
    noun = "Asset Type"


class AssetTypeDelete(company_writes.EntityDeleteView):
    model = AssetType
    page_code = "fleet.asset_type"
    noun = "Asset Type"


class AssetListView(FleetScreenView):
    template_name = "fleet/asset_list.html"
    page_code = "fleet.asset"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        assets = (
            scoping.assets_for(self.request.user)
            .select_related("asset_type", "brand", "custodian", "branch")
        )
        for i, asset in enumerate(assets):
            rows.append({
                "code": asset.asset_code,
                "asset_type": asset.asset_type.asset_type_name,
                "brand": asset.brand.brand_name if asset.brand_id else "",
                "custodian": asset.custodian.full_name if asset.custodian_id else "",
                "branch": asset.branch.name if asset.branch_id else "",
                "active": asset.is_active,
                "pk": asset.pk,
                "json_id": f"scr-record-asset-{i}",
                "fields_json": {
                    "pk": asset.pk,
                    "company": asset.company_id,
                    "asset_code": asset.asset_code,
                    "asset_type": asset.asset_type_id,
                    "brand": asset.brand_id or "",
                    "serial_no": asset.serial_no,
                    "manufacturer": asset.manufacturer,
                    "custodian": asset.custodian_id or "",
                    "supplier": asset.supplier,
                    "purchase_invoice_no": asset.purchase_invoice_no,
                    "description": asset.description,
                    "warranty_from_date": (
                        asset.warranty_from_date.isoformat() if asset.warranty_from_date else ""
                    ),
                    "warranty_to_date": (
                        asset.warranty_to_date.isoformat() if asset.warranty_to_date else ""
                    ),
                    "branch": asset.branch_id or "",
                    "is_active": asset.is_active,
                },
            })
        context["assets"] = rows
        context["save_url_asset"] = reverse("fleet-asset-save")
        context["delete_url_asset"] = reverse("fleet-asset-delete", args=[0])
        context["noun_asset"] = "Asset"
        return context


class AssetSave(company_writes.EntitySaveView):
    model = Asset
    form_class = forms.AssetForm
    page_code = "fleet.asset"
    noun = "Asset"


class AssetDelete(company_writes.EntityDeleteView):
    model = Asset
    page_code = "fleet.asset"
    noun = "Asset"


class VehicleListView(FleetScreenView):
    template_name = "fleet/vehicle_list.html"
    page_code = "fleet.vehicle"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        vehicles = (
            scoping.vehicles_for(self.request.user).select_related("vehicle_type", "uom", "current_branch")
        )
        for i, vehicle in enumerate(vehicles):
            rows.append({
                "code": vehicle.vehicle_code,
                "name": vehicle.vehicle_name,
                "vehicle_type": vehicle.vehicle_type.vehicle_type_name,
                "uom": vehicle.uom.uom_name,
                "rfid_epc": vehicle.rfid_epc,
                "branch": vehicle.current_branch.name if vehicle.current_branch_id else "",
                "available": vehicle.is_available,
                "active": vehicle.is_active,
                "pk": vehicle.pk,
                "json_id": f"scr-record-vehicle-{i}",
                "fields_json": {
                    "pk": vehicle.pk,
                    "company": vehicle.company_id,
                    "vehicle_code": vehicle.vehicle_code,
                    "vehicle_name": vehicle.vehicle_name,
                    "vehicle_type": vehicle.vehicle_type_id,
                    "uom": vehicle.uom_id,
                    "rfid_epc": vehicle.rfid_epc,
                    "current_branch": vehicle.current_branch_id or "",
                    "is_available": vehicle.is_available,
                    "is_active": vehicle.is_active,
                },
            })
        context["vehicles"] = rows
        context["save_url_vehicle"] = reverse("fleet-vehicle-save")
        context["delete_url_vehicle"] = reverse("fleet-vehicle-delete", args=[0])
        context["noun_vehicle"] = "Vehicle"
        return context


class VehicleSave(company_writes.EntitySaveView):
    model = Vehicle
    form_class = forms.VehicleForm
    page_code = "fleet.vehicle"
    noun = "Vehicle"


class VehicleDelete(company_writes.EntityDeleteView):
    model = Vehicle
    page_code = "fleet.vehicle"
    noun = "Vehicle"


class FleetPrivilegeView(PrivilegeScreenView):
    """Inventory's copy of the shared privilege grid."""

    page_code = "fleet.privileges"
    module_code = "fleet"
    module_label = "Vehicle Management"
