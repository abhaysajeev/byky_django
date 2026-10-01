"""Rental Management screens: the Customer list + drawer."""

from django.urls import reverse

from apps.company import writes as company_writes
from apps.company.scoping import companies_for
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from apps.rental import drawers, scoping
from apps.rental.models import Customer, Gender, IdType
from core.ordering import recent_first
from theme import drawers as theme_drawers
from theme.views import ThemedTemplateView

ACTIONS = ("create", "read", "update", "delete", "print")


class RentalScreenView(PagePermissionMixin, ThemedTemplateView):
    """Shared by every rental screen: the lists its drawers resolve against,
    and the permission flags its buttons check."""

    drawer_specs = drawers.SPECS

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update({
            "companies_list": list(companies_for(user).filter(is_active=True).values("id", "name")),
            "genders_list": [{"id": value, "name": label} for value, label in Gender.choices],
            "id_types_list": [{"id": value, "name": label} for value, label in IdType.choices],
            "perm": {action: has_permission(user, self.page_code, action) for action in ACTIONS},
        })
        return context

    def render_to_response(self, context, **response_kwargs):
        specs = theme_drawers.all_for_user(self.drawer_specs, self.request.user.sees_every_company)
        context.update(theme_drawers.resolve_all(specs, context))
        return super().render_to_response(context, **response_kwargs)


class CustomerListView(RentalScreenView):
    template_name = "rental/customer_list.html"
    page_code = "rental.customer"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        customers = recent_first(scoping.customers_for(self.request.user)).select_related("company")

        rows = []
        for i, customer in enumerate(customers):
            rows.append({
                "pk": customer.pk,
                "code": customer.customer_code,
                "name": customer.full_name,
                "id_no": customer.id_no,
                "mobile": f"{customer.mobile_country_code} {customer.mobile_no}".strip(),
                "email": customer.email,
                "status": "Blocked" if customer.is_blocked else "Active",
                "active": customer.is_active,
                "json_id": f"scr-record-customer-{i}",
                "fields_json": self._fields_json(customer),
            })

        context.update({
            "customers": rows,
            "active_count": sum(1 for row in rows if row["status"] == "Active"),
            "blocked_count": sum(1 for row in rows if row["status"] == "Blocked"),
            "save_url_customer": reverse("rental-customer-save"),
            "delete_url_customer": reverse("rental-customer-delete", args=[0]),
            "noun_customer": "Customer",
        })
        return context

    @staticmethod
    def _fields_json(customer):
        return {
            "pk": customer.pk,
            "company": customer.company_id,
            "first_name": customer.first_name,
            "last_name": customer.last_name,
            "gender": customer.gender,
            "date_of_birth": customer.date_of_birth.isoformat() if customer.date_of_birth else "",
            "nationality": customer.nationality,
            "id_type": customer.id_type,
            "id_no": customer.id_no,
            "mobile_country_code": customer.mobile_country_code,
            "mobile_no": customer.mobile_no,
            "mobile_full": customer.mobile_full,
            "email": customer.email,
            "address": customer.address,
            "remarks": customer.remarks,
            "is_blocked": customer.is_blocked,
            "block_reason": customer.block_reason,
            "is_active": customer.is_active,
        }


class CustomerDelete(company_writes.EntityDeleteView):
    model = Customer
    page_code = "rental.customer"
    noun = "Customer"


class RentalPrivilegeView(PrivilegeScreenView):
    """Rental Management's copy of the shared privilege grid."""

    page_code = "rental.privileges"
    module_code = "rental"
    module_label = "Rental Management"
