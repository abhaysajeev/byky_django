"""Rental Management screens: the Orders list and detail (read-only), and the
Customer and Payment Mode lists + drawers."""

import datetime
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.urls import reverse

from apps.company import writes as company_writes
from apps.company.models import PaymentMode
from apps.company.scoping import branches_for, companies_for, payment_modes_for
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from apps.rental import drawers, forms, scoping
from apps.rental.models import (
    Customer,
    Gender,
    IdType,
    Invoice,
    OrderAction,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    PaymentKind,
)
from core.ordering import recent_first
from core.timezones import business_date_for, zone_for
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


class PaymentModeListView(RentalScreenView):
    """How a customer may pay. The operator app offers the active ones at
    checkout (POST /api/v1/{app}/payment-modes)."""

    template_name = "rental/payment_mode_list.html"
    page_code = "rental.payment_mode"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        modes = recent_first(payment_modes_for(self.request.user)).select_related("company")

        rows = []
        for i, mode in enumerate(modes):
            rows.append({
                "pk": mode.pk,
                "name": mode.name,
                "company": mode.company.name,
                "active": mode.is_active,
                "json_id": f"scr-record-payment-mode-{i}",
                "fields_json": {
                    "pk": mode.pk,
                    "company": mode.company_id,
                    "name": mode.name,
                    "is_active": mode.is_active,
                },
            })

        context.update({
            "payment_modes": rows,
            "show_company": self.request.user.sees_every_company,
            "save_url_payment_mode": reverse("rental-payment-mode-save"),
            "delete_url_payment_mode": reverse("rental-payment-mode-delete", args=[0]),
            "noun_payment_mode": "Payment Mode",
        })
        return context


class PaymentModeSave(company_writes.EntitySaveView):
    model = PaymentMode
    form_class = forms.PaymentModeForm
    page_code = "rental.payment_mode"
    noun = "Payment Mode"


class PaymentModeDelete(company_writes.EntityDeleteView):
    """Orders and payments point at a mode (PROTECT), so a used one is
    switched off rather than removed."""

    model = PaymentMode
    page_code = "rental.payment_mode"
    noun = "Payment Mode"


class RentalPrivilegeView(PrivilegeScreenView):
    """Rental Management's copy of the shared privilege grid."""

    page_code = "rental.privileges"
    module_code = "rental"
    module_label = "Rental Management"


# -- Orders (read-only: orders change only from the tablet) -----------------------

ORDERS_PER_PAGE = 50

# The order's state as the back office reads it. "Awaiting settlement" -- every
# vehicle back, the bill not closed -- is derived, never stored.
ORDER_STATUSES = [
    ("active", "Active"), ("awaiting", "Awaiting settlement"),
    ("completed", "Completed"), ("cancelled", "Cancelled"),
]
_STATUS_LABEL = dict(ORDER_STATUSES)


def _status_key(order, items_out):
    if order.status == OrderStatus.ACTIVE:
        return "active" if items_out else "awaiting"
    return order.status


def _aed(value):
    return f"AED {value:,.2f}" if value is not None else None


def _day_bounds(day, zone):
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=zone)
    return start, start + datetime.timedelta(days=1)


def _date_param(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _orders(request, zone):
    """The orders the list shows, narrowed by the query string."""
    params = request.GET
    active_items = Count("items", filter=Q(items__status=OrderItemStatus.ACTIVE))
    orders = (scoping.orders_for(request.user).select_related("branch", "device")
              .annotate(items_out=active_items))
    text = params.get("q", "").strip()
    if text:
        orders = orders.filter(Q(order_no__icontains=text) | Q(customer_name__icontains=text)
                               | Q(customer_mobile__icontains=text))
    if params.get("branch", "").isdigit():
        orders = orders.filter(branch_id=int(params["branch"]))
    status = params.get("status", "")
    if status == "active":
        orders = orders.filter(status=OrderStatus.ACTIVE, items_out__gt=0)
    elif status == "awaiting":
        orders = orders.filter(status=OrderStatus.ACTIVE, items_out=0)
    elif status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
        orders = orders.filter(status=status)
    start, end = _date_param(params.get("from")), _date_param(params.get("to"))
    if start:
        orders = orders.filter(booked_at__gte=_day_bounds(start, zone)[0])
    if end:
        orders = orders.filter(booked_at__lt=_day_bounds(end, zone)[1])
    return orders.order_by("-booked_at")


def _order_kpis(user, branch_id, zone, company):
    """Right now and today, for the whole scope or one station -- never
    narrowed by the list's status or date filters."""
    orders = scoping.orders_for(user)
    items = OrderItem.objects.filter(order__in=orders)
    if branch_id:
        orders, items = orders.filter(branch_id=branch_id), items.filter(order__branch_id=branch_id)
    start, end = _day_bounds(business_date_for(company), zone)
    settled = orders.filter(status=OrderStatus.COMPLETED, completed_at__gte=start, completed_at__lt=end)
    return [
        {"label": "Active orders now", "value": orders.filter(status=OrderStatus.ACTIVE).count()},
        {"label": "Vehicles out now", "value": items.filter(status=OrderItemStatus.ACTIVE).count()},
        {"label": "Settled today", "value": settled.count()},
        {"label": "Settled revenue today",
         "value": _aed(settled.aggregate(total=Sum("net_amount"))["total"] or Decimal("0"))},
    ]


def _order_row(order):
    status = _status_key(order, order.items_out)
    return {
        "order_no": order.order_no, "booked_at": order.booked_at,
        "customer": order.customer_name, "mobile": order.customer_mobile,
        "station": order.branch.name, "tablet": order.device.device_registration_id,
        "status": status, "status_label": _STATUS_LABEL[status], "is_direct": order.is_direct_bill,
        "detail_url": reverse("rental-order-detail", args=[order.pk]),
    }


class OrderListView(RentalScreenView):
    template_name = "rental/order_list.html"
    page_code = "rental.order"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user, params = self.request.user, self.request.GET
        company = getattr(user, "company", None)
        zone = zone_for(company)
        page = Paginator(_orders(self.request, zone), ORDERS_PER_PAGE).get_page(params.get("page"))
        query = params.copy()
        query.pop("page", None)
        branch_id = int(params["branch"]) if params.get("branch", "").isdigit() else None
        context.update({
            "kpis": _order_kpis(user, branch_id, zone, company),
            "rows": [_order_row(order) for order in page.object_list],
            "page": page, "query": query.urlencode(), "params": params,
            "filtered": any(params.get(k) for k in ("q", "branch", "status", "from", "to")),
            "statuses": ORDER_STATUSES,
            "branches": list(branches_for(user).filter(is_active=True).order_by("name").values("id", "name")),
        })
        return context


_ACTION_LABEL = dict(OrderAction.choices)


def _event_summary(event):
    """One line for the history: what the call did, from its own record."""
    detail, action = event.detail or {}, event.action
    vehicle = event.item.vehicle.vehicle_name if event.item_id else ""
    new_vehicle = event.new_item.vehicle.vehicle_name if event.new_item_id else ""
    if action == OrderAction.BOOK:
        return f"{detail.get('items', 0)} vehicle(s) · advance AED {detail.get('advance', '0.00')}"
    if action == OrderAction.ADD:
        return f"{new_vehicle} · advance AED {detail.get('advance', '0.00')}"
    if action == OrderAction.REPLACE:
        return f"{vehicle} → {new_vehicle} · {detail.get('reason', '')}"
    if action == OrderAction.REMOVE:
        return f"{vehicle} · {detail.get('reason', '')}"
    if action == OrderAction.RETURN:
        return f"{vehicle} · {detail.get('run_minutes')} min · AED {detail.get('total_amount')}"
    if action == OrderAction.PAYMENT:
        return f"{str(detail.get('kind', '')).capitalize()} AED {detail.get('total')} · {detail.get('count')} entr" \
               f"{'y' if detail.get('count') == 1 else 'ies'}"
    if action == OrderAction.SETTLE:
        return f"Net AED {detail.get('net_amount')} · discount AED {detail.get('discount_amount')}"
    return ""


class OrderDetailView(RentalScreenView):
    template_name = "rental/order_detail.html"
    page_code = "rental.order"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        zone = zone_for(getattr(user, "company", None))
        order = (scoping.orders_for(user)
                 .select_related("branch", "device", "created_by", "discount_claim__card_type",
                                 "discount_claim__card_grade")
                 .filter(pk=self.kwargs["pk"]).first())
        if order is None:
            raise Http404("No such order.")

        items = list(order.items.select_related("vehicle__vehicle_type").order_by("start_time", "created_on"))
        replaced_by = {item.replaced_item_id: item for item in items if item.replaced_item_id}
        items_out = sum(1 for item in items if item.status == OrderItemStatus.ACTIVE)
        status = _status_key(order, items_out)
        invoice = Invoice.objects.filter(order=order).select_related("issued_by").first()
        claim = order.discount_claim

        def local(moment):
            return moment.astimezone(zone) if moment else None

        context.update({
            "order": order, "status": status, "status_label": _STATUS_LABEL[status],
            "booked_at": local(order.booked_at), "start_time": local(order.start_time),
            "completed_at": local(order.completed_at), "cancelled_at": local(order.cancelled_at),
            "operator": order.created_by.display_name if order.created_by_id else "",
            "tablet": order.device.device_registration_id,
            "discount_card": f"{claim.card_type.name} · {claim.card_grade.name}" if claim else "",
            "items": [
                {
                    "vehicle": item.vehicle.vehicle_name, "identifier": item.vehicle.identifier,
                    "vehicle_type": item.vehicle.vehicle_type.vehicle_type_name,
                    "status": item.status, "status_label": item.get_status_display(),
                    "package_minutes": item.package_minutes, "run_minutes": item.run_minutes,
                    "start_time": local(item.start_time), "expected_end_time": local(item.expected_end_time),
                    "end_time": local(item.end_time),
                    "base_fare": item.base_fare, "overtime_amount": item.overtime_amount,
                    "total_amount": item.total_amount, "reason": item.reason,
                    "replaced_by": replaced_by[item.pk].vehicle.vehicle_name if item.pk in replaced_by else "",
                }
                for item in items
            ],
            "items_out": items_out,
            "payments": [
                {
                    "paid_at": local(payment.paid_at), "kind": payment.kind, "kind_label": payment.get_kind_display(),
                    "mode": payment.mode.name, "reference_no": payment.reference_no,
                    "reference_date": payment.reference_date,
                    "amount": -payment.amount if payment.kind == PaymentKind.REFUND else payment.amount,
                }
                for payment in order.payments.select_related("mode").order_by("paid_at", "created_on")
            ],
            "invoice": {
                "invoice_no": invoice.invoice_no, "issued_at": local(invoice.issued_at),
                "issued_by": invoice.issued_by.display_name, "net_amount": invoice.net_amount,
            } if invoice else None,
            "history": [
                {
                    "label": _ACTION_LABEL.get(event.action, event.action), "at": local(event.happened_at),
                    "who": event.user.display_name,
                    "tablet": event.device.device_registration_id if event.device_id else "",
                    "summary": _event_summary(event),
                }
                for event in order.events.select_related("user", "device", "item__vehicle", "new_item__vehicle")
                .order_by("happened_at", "received_at")
            ],
            "list_url": reverse("rental-order-list"),
        })
        return context
