"""Vehicle Management screens.

Follows the same shape as apps/company's screens: a shared *ScreenView base
per module, a generic drawer resolved in render_to_response, and the write
endpoints reused straight from apps.company.writes rather than redefined here
-- Brand needs nothing that generic save/delete doesn't already do.
"""

from django.urls import reverse

from apps.company import writes as company_writes
from apps.company.scoping import companies_for
from apps.fleet import drawers, forms, scoping
from apps.fleet.models import Brand, Category, VehicleType
from apps.portal.permissions import PagePermissionMixin
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
