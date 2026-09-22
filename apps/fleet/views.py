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
from apps.fleet.models import Brand
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
