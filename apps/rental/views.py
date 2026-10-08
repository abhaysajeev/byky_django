"""Rental Management screens: the Orders and Invoices lists and details
(read-only, with the invoice print preview), and the Customer and Payment
Mode lists + drawers."""

import datetime
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.company import writes as company_writes
from apps.company.models import PaymentMode
from apps.company.scoping import branches_for, companies_for, payment_modes_for
from apps.devices import services as devices_services
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from apps.rental import drawers, forms, scoping
from apps.rental.models import (
    CREDIT_NOTE_LIVE,
    CreditNoteSource,
    CreditNoteStatus,
    Customer,
    Gender,
    IdType,
    Invoice,
    OrderAction,
    OrderItem,
    OrderItemStatus,
    OrderRequestStatus,
    OrderStatus,
    PaymentKind,
)
from core.ordering import recent_first
from core.timezones import business_date_for, zone_for
from theme import drawers as theme_drawers
from theme.views import ThemedTemplateView

ACTIONS = ("create", "read", "update", "delete", "print", "approve")


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
    return f"AED {value:,.3f}" if value is not None else None


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
            "credit_note": credit_note_link(order),
            "discount_request": discount_request_link(order),
            "list_url": reverse("rental-order-list"),
        })
        return context


def discount_request_link(order):
    """The order's latest discount request, for the order page: the approved
    rule (or its state) and its page."""
    req = order.requests.order_by("-requested_at").first()
    if req is None:
        return None
    return {"label": rule_label(req) or req.get_kind_display(), "status_label": req.get_status_display(),
            "applied_amount": req.applied_amount, "url": reverse("rental-request-detail", args=[req.pk])}


def credit_note_link(order):
    """The order's credit note -- waiting or issued -- for the order and
    invoice pages: number (or "Requested"), amount, status and its page."""
    note = order.credit_notes.filter(status__in=CREDIT_NOTE_LIVE).first()
    if note is None:
        return None
    return {
        "label": note.credit_note_no or "Requested", "status": note.status, "status_label": note.get_status_display(),
        "net_amount": note.net_amount, "tax_amount": note.tax_amount,
        "url": reverse("rental-credit-note-detail", args=[note.pk]),
    }


# -- Invoices (read-only: issued at settle, never edited) ---------------------------

INVOICES_PER_PAGE = 50


def _hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}" if minutes is not None else "—"


def _clock(moment):
    return moment.strftime("%I:%M %p") if moment else "—"


def _rate(amount, minutes):
    return f"{amount:.3f}/{minutes} MINS"


def _receipt(invoice, zone):
    """The printed tax invoice, laid out as the station's receipt -- one dict
    for the print preview and the detail page. Everything comes from the
    invoice's own copy, frozen at issue; only what it does not hold is read
    from the order (who opened it, the customer's ID, the fare's extra rate)."""
    order = invoice.order

    def local(moment):
        return moment.astimezone(zone) if moment else None

    items = list(invoice.items.select_related("order_item__fare").order_by("start_time"))
    lines = []
    for number, item in enumerate(items, start=1):
        fare = item.order_item.fare
        lines.append({
            "sno": f"{number:02d}", "vehicle": item.vehicle_name, "identifier": item.vehicle_identifier,
            "vehicle_type": item.vehicle_type, "package_minutes": item.package_minutes,
            "run_minutes": item.run_minutes,
            "base_rate": _rate(item.base_fare, item.package_minutes),
            "extra_rate": _rate(fare.concurrent_fare, fare.concurrent_interval_minutes) if fare else "—",
            "start_time": local(item.start_time), "end_time": local(item.end_time),
            "returned": _clock(local(item.end_time)), "duration": _hhmm(item.run_minutes),
            "base_fare": item.base_fare, "overtime_amount": item.overtime_amount, "amount": item.total_amount,
        })
    returned = max((item.end_time for item in items if item.end_time), default=None)
    run = sum(item.run_minutes or 0 for item in items) if items else None
    customer = order.customer
    number = devices_services.running_number(order.device, order.branch, invoice.invoice_no)
    issued = local(invoice.issued_at)
    return {
        "invoice_no": invoice.invoice_no, "issued_at": issued, "date": issued.strftime("%d-%m-%Y"),
        "trans_no": number if number is not None else "—",
        "station": invoice.branch_name, "emirate": invoice.branch.location.state.name,
        "company": invoice.company_name, "trn": invoice.company_trn or "—",
        "open_by": order.created_by.display_name if order.created_by_id else "—",
        "closed_by": invoice.issued_by.display_name,
        "starting_time": _clock(local(order.start_time)), "returning_time": _clock(local(returned)),
        "closing_time": _clock(issued),
        "lines": lines,
        "subtotal": invoice.subtotal, "discount_percentage": invoice.discount_percentage,
        "discount_amount": invoice.discount_amount, "taxable_amount": invoice.net_amount - invoice.tax_amount,      # VAT is inside the net
        "tax_percentage": invoice.tax_percentage, "tax_amount": invoice.tax_amount,
        "rounding": invoice.rounding_adjustment, "net_amount": invoice.net_amount,
        "customer": invoice.customer_name or "—", "mobile": invoice.customer_mobile or "—",
        "id_type": customer.get_id_type_display() if customer.id_type else "—", "id_no": customer.id_no or "—",
        "balance": invoice.net_amount - order.paid_amount, "time": _hhmm(run),
        "payments": invoice.payments,
        "order_no": order.order_no, "tablet": invoice.device.device_registration_id,
        "receipt_url": reverse("rental-invoice-receipt", args=[invoice.pk]),
        "credit_note": credit_note_link(order),
    }


def _invoices_for_receipt(user):
    return scoping.invoices_for(user).select_related(
        "order__customer", "order__created_by", "order__device", "order__branch", "branch__location__state",
        "device", "issued_by",
    )


def _invoices(request, zone):
    """The invoices the list shows, narrowed by the query string."""
    params = request.GET
    invoices = scoping.invoices_for(request.user).select_related("device", "issued_by")
    text = params.get("q", "").strip()
    if text:
        invoices = invoices.filter(Q(invoice_no__icontains=text) | Q(customer_name__icontains=text)
                                   | Q(customer_mobile__icontains=text))
    if params.get("branch", "").isdigit():
        invoices = invoices.filter(branch_id=int(params["branch"]))
    start, end = _date_param(params.get("from")), _date_param(params.get("to"))
    if start:
        invoices = invoices.filter(issued_at__gte=_day_bounds(start, zone)[0])
    if end:
        invoices = invoices.filter(issued_at__lt=_day_bounds(end, zone)[1])
    return invoices.order_by("-issued_at")


class InvoiceListView(RentalScreenView):
    template_name = "rental/invoice_list.html"
    page_code = "rental.invoice"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user, params = self.request.user, self.request.GET
        zone = zone_for(getattr(user, "company", None))
        page = Paginator(_invoices(self.request, zone), INVOICES_PER_PAGE).get_page(params.get("page"))
        query = params.copy()
        query.pop("page", None)
        context.update({
            "rows": [
                {
                    "invoice_no": invoice.invoice_no, "issued_at": invoice.issued_at.astimezone(zone),
                    "customer": invoice.customer_name, "mobile": invoice.customer_mobile,
                    "station": invoice.branch_name, "tablet": invoice.device.device_registration_id,
                    "issued_by": invoice.issued_by.display_name,
                    "net_amount": invoice.net_amount, "tax_amount": invoice.tax_amount,
                    "receipt_url": reverse("rental-invoice-receipt", args=[invoice.pk]),
                    "detail_url": reverse("rental-invoice-detail", args=[invoice.pk]),
                }
                for invoice in page.object_list
            ],
            "page": page, "query": query.urlencode(), "params": params,
            "filtered": any(params.get(k) for k in ("q", "branch", "from", "to")),
            "branches": list(branches_for(user).filter(is_active=True).order_by("name").values("id", "name")),
        })
        return context


class InvoiceReceiptView(PagePermissionMixin, View):
    """The receipt alone -- an HTML fragment the print preview loads."""

    page_code = "rental.invoice"

    def get(self, request, pk):
        invoice = _invoices_for_receipt(request.user).filter(pk=pk).first()
        if invoice is None:
            raise Http404("No such invoice.")
        bill = _receipt(invoice, zone_for(getattr(request.user, "company", None)))
        return render(request, "rental/partials/invoice_receipt.html", {"bill": bill})


class InvoiceDetailView(RentalScreenView):
    template_name = "rental/invoice_detail.html"
    page_code = "rental.invoice"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        invoice = _invoices_for_receipt(self.request.user).filter(pk=self.kwargs["pk"]).first()
        if invoice is None:
            raise Http404("No such invoice.")
        bill = _receipt(invoice, zone_for(getattr(self.request.user, "company", None)))
        context.update({
            "bill": bill,
            "list_url": reverse("rental-invoice-list"),
            "order_url": reverse("rental-order-detail", args=[invoice.order_id]),
            "can_issue_credit_note": bill["credit_note"] is None
            and has_permission(self.request.user, "rental.credit_note", "create"),
            "issue_credit_note_url": reverse("rental-credit-note-issue", args=[invoice.pk]),
            "issue_max": invoice.order.net_amount,
            "issue_tax_percentage": invoice.tax_percentage,
        })
        return context


# -- Credit notes (requested on a tablet or issued here; apps/rental/credit_notes.py) --

CREDIT_NOTES_PER_PAGE = 50
# The list's tabs, one per state -- as the Card Discount Approval screen.
CREDIT_NOTE_TABS = [
    (CreditNoteStatus.PENDING, "Pending"), (CreditNoteStatus.APPROVED, "Approved"),
    (CreditNoteStatus.REJECTED, "Rejected"), (CreditNoteStatus.CANCELLED, "Cancelled"),
]


def _credit_notes(request, zone):
    """The credit notes the list's filters match, every state -- the tab
    picks the state, so its counts come from the same filters."""
    params = request.GET
    notes = scoping.credit_notes_for(request.user).select_related("order", "invoice", "branch")
    text = params.get("q", "").strip()
    if text:
        notes = notes.filter(Q(order__order_no__icontains=text) | Q(credit_note_no__icontains=text)
                             | Q(invoice__customer_name__icontains=text)
                             | Q(invoice__customer_mobile__icontains=text))
    if params.get("source") in CreditNoteSource.values:
        notes = notes.filter(source=params["source"])
    if params.get("branch", "").isdigit():
        notes = notes.filter(branch_id=int(params["branch"]))
    start, end = _date_param(params.get("from")), _date_param(params.get("to"))
    if start:
        notes = notes.filter(created_on__gte=_day_bounds(start, zone)[0])
    if end:
        notes = notes.filter(created_on__lt=_day_bounds(end, zone)[1])
    return notes


class CreditNoteListView(RentalScreenView):
    template_name = "rental/credit_note_list.html"
    page_code = "rental.credit_note"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user, params = self.request.user, self.request.GET
        zone = zone_for(getattr(user, "company", None))
        tab = params.get("tab", CreditNoteStatus.PENDING)
        if tab not in dict(CREDIT_NOTE_TABS):
            tab = CreditNoteStatus.PENDING
        notes = _credit_notes(self.request, zone)
        counts = dict(notes.order_by().values_list("status").annotate(n=Count("pk")))
        page = Paginator(notes.filter(status=tab).order_by("-created_on"),
                         CREDIT_NOTES_PER_PAGE).get_page(params.get("page"))
        # Tabs keep the other filters; the pager keeps the tab too.
        filters = params.copy()
        for key in ("page", "tab"):
            filters.pop(key, None)
        query = filters.copy()
        query["tab"] = tab
        context.update({
            "tab": tab,
            "tab_label": dict(CREDIT_NOTE_TABS)[tab],
            "tabs": [{"value": value, "label": label, "count": counts.get(value, 0)}
                     for value, label in CREDIT_NOTE_TABS],
            "filter_query": filters.urlencode(),
            "rows": [
                {
                    "number": note.credit_note_no or "Request", "created_on": note.created_on.astimezone(zone),
                    "invoice_no": note.invoice.invoice_no, "customer": note.invoice.customer_name,
                    "mobile": note.invoice.customer_mobile, "station": note.branch.name,
                    "source": note.source, "source_label": "Tablet" if note.source == CreditNoteSource.DEVICE
                    else "Web", "net_amount": note.net_amount, "status": note.status,
                    "status_label": note.get_status_display(),
                    "detail_url": reverse("rental-credit-note-detail", args=[note.pk]),
                }
                for note in page.object_list
            ],
            "page": page, "query": query.urlencode(), "params": params,
            "filtered": any(params.get(k) for k in ("q", "source", "branch", "from", "to")),
            "branches": list(branches_for(user).filter(is_active=True).order_by("name").values("id", "name")),
        })
        return context


def _credit_note_for(user, pk):
    note = (scoping.credit_notes_for(user)
            .select_related("order", "invoice", "branch", "device", "requested_by", "decided_by", "created_by")
            .filter(pk=pk).first())
    if note is None:
        raise Http404("No such credit note.")
    return note


class CreditNoteDetailView(RentalScreenView):
    template_name = "rental/credit_note_detail.html"
    page_code = "rental.credit_note"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        zone = zone_for(getattr(user, "company", None))
        note = _credit_note_for(user, self.kwargs["pk"])
        order, invoice = note.order, note.invoice

        def local(moment):
            return moment.astimezone(zone) if moment else None

        context.update({
            "note": note,
            "requested_at": local(note.requested_at), "decided_at": local(note.decided_at),
            "created_on": local(note.created_on),
            "requested_by": note.requested_by.display_name if note.requested_by_id else "",
            "tablet": note.device.device_registration_id if note.device_id else "",
            "decided_by": note.decided_by.display_name if note.decided_by_id else "",
            "invoice": invoice, "invoice_issued_at": local(invoice.issued_at), "order": order,
            "paid_amount": order.paid_amount,
            "invoice_url": reverse("rental-invoice-detail", args=[invoice.pk]),
            "order_url": reverse("rental-order-detail", args=[order.pk]),
            "list_url": reverse("rental-credit-note-list"),
            "approve_url": reverse("rental-credit-note-approve", args=[note.pk]),
            "reject_url": reverse("rental-credit-note-reject", args=[note.pk]),
            "receipt_url": reverse("rental-credit-note-receipt", args=[note.pk]),
            "max_amount": order.net_amount,
        })
        return context


class CreditNoteReceiptView(PagePermissionMixin, View):
    """The printed credit note alone -- an HTML fragment for the print preview."""

    page_code = "rental.credit_note"
    required_action = "print"

    def get(self, request, pk):
        note = _credit_note_for(request.user, pk)
        if note.status != CreditNoteStatus.APPROVED:
            raise Http404("Only an issued credit note prints.")
        zone = zone_for(getattr(request.user, "company", None))
        invoice = note.invoice
        return render(request, "rental/partials/credit_note_receipt.html", {
            "note": note, "invoice": invoice, "date": note.decided_at.astimezone(zone).strftime("%d-%m-%Y"),
            "invoice_date": invoice.issued_at.astimezone(zone).strftime("%d-%m-%Y"),
            "emirate": invoice.branch.location.state.name, "issued_by": note.decided_by.display_name
            if note.decided_by_id else "—",
        })



# -- Requests (operator asks on a tablet, a manager decides; apps/rental/requests.py) --

REQUESTS_PER_PAGE = 50
# One tab per state; Closed holds both endings that were nobody's decision --
# withdrawn by the tablet and closed by the settle.
REQUEST_TABS = [
    ("pending", "Pending", (OrderRequestStatus.PENDING,)),
    ("approved", "Approved", (OrderRequestStatus.APPROVED,)),
    ("rejected", "Rejected", (OrderRequestStatus.REJECTED,)),
    ("revoked", "Revoked", (OrderRequestStatus.REVOKED,)),
    ("closed", "Closed", (OrderRequestStatus.WITHDRAWN, OrderRequestStatus.CLOSED)),
]


def rule_label(req):
    """An approved discount as one phrase: "10.00%" or "AED 15.000"."""
    if req.discount_value is None:
        return ""
    if req.discount_type == "percent":
        return f"{req.discount_value.quantize(Decimal('0.01'))}%"
    return f"AED {req.discount_value}"


def _requests(request, zone):
    """The requests the list's filters match, every state -- the tab picks the
    state, so its counts come from the same filters."""
    params = request.GET
    rows = scoping.requests_for(request.user).select_related("order", "branch", "requested_by")
    text = params.get("q", "").strip()
    if text:
        rows = rows.filter(Q(order__order_no__icontains=text) | Q(order__customer_name__icontains=text)
                           | Q(order__customer_mobile__icontains=text))
    if params.get("branch", "").isdigit():
        rows = rows.filter(branch_id=int(params["branch"]))
    start, end = _date_param(params.get("from")), _date_param(params.get("to"))
    if start:
        rows = rows.filter(requested_at__gte=_day_bounds(start, zone)[0])
    if end:
        rows = rows.filter(requested_at__lt=_day_bounds(end, zone)[1])
    return rows


class RequestListView(RentalScreenView):
    template_name = "rental/request_list.html"
    page_code = "rental.request"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user, params = self.request.user, self.request.GET
        zone = zone_for(getattr(user, "company", None))
        tabs = {value: (label, states) for value, label, states in REQUEST_TABS}
        tab = params.get("tab", "pending")
        if tab not in tabs:
            tab = "pending"
        rows = _requests(self.request, zone)
        by_status = dict(rows.order_by().values_list("status").annotate(n=Count("pk")))
        # Waiting ones oldest first -- the longest wait is answered first.
        ordering = "requested_at" if tab == "pending" else "-requested_at"
        page = Paginator(rows.filter(status__in=tabs[tab][1]).order_by(ordering),
                         REQUESTS_PER_PAGE).get_page(params.get("page"))
        filters = params.copy()
        for key in ("page", "tab"):
            filters.pop(key, None)
        query = filters.copy()
        query["tab"] = tab
        context.update({
            "tab": tab, "tab_label": tabs[tab][0],
            "tabs": [{"value": value, "label": label, "count": sum(by_status.get(s, 0) for s in states)}
                     for value, label, states in REQUEST_TABS],
            "filter_query": filters.urlencode(),
            "rows": [
                {
                    "requested_at": req.requested_at.astimezone(zone), "kind": req.get_kind_display(),
                    "order_no": req.order.order_no, "order_url": reverse("rental-order-detail", args=[req.order_id]),
                    "customer": req.order.customer_name, "mobile": req.order.customer_mobile,
                    "station": req.branch.name,
                    "requested_by": req.requested_by.display_name if req.requested_by_id else "",
                    "reason": req.reason, "rule": rule_label(req), "applied_amount": req.applied_amount,
                    "status": req.status,
                    "detail_url": reverse("rental-request-detail", args=[req.pk]),
                }
                for req in page.object_list
            ],
            "page": page, "query": query.urlencode(), "params": params,
            "filtered": any(params.get(k) for k in ("q", "branch", "from", "to")),
            "branches": list(branches_for(user).filter(is_active=True).order_by("name").values("id", "name")),
        })
        return context


class RequestDetailView(RentalScreenView):
    template_name = "rental/request_detail.html"
    page_code = "rental.request"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        zone = zone_for(getattr(user, "company", None))
        req = (scoping.requests_for(user)
               .select_related("order", "branch", "device", "requested_by", "decided_by", "revoked_by")
               .filter(pk=self.kwargs["pk"]).first())
        if req is None:
            raise Http404("No such request.")
        order = req.order

        def local(moment):
            return moment.astimezone(zone) if moment else None

        now = timezone.now()
        lines = []
        for item in order.items.select_related("vehicle__vehicle_type").order_by("start_time", "created_on"):
            if item.status not in (OrderItemStatus.ACTIVE, OrderItemStatus.RETURNED):
                continue
            end = item.end_time or now
            lines.append({
                "vehicle": item.vehicle.vehicle_name, "identifier": item.vehicle.identifier,
                "vehicle_type": item.vehicle.vehicle_type.vehicle_type_name, "status": item.status,
                "status_label": item.get_status_display(), "start_time": local(item.start_time),
                "end_time": local(item.end_time),
                "minutes": max(int((end - item.start_time).total_seconds() // 60), 0),
            })
        context.update({
            "req": req, "order": order, "lines": lines, "rule": rule_label(req),
            "order_running": order.status == OrderStatus.ACTIVE,
            "requested_at": local(req.requested_at), "decided_at": local(req.decided_at),
            "revoked_at": local(req.revoked_at), "closed_at": local(req.closed_at),
            "applied_at": local(req.applied_at), "booked_at": local(order.booked_at),
            "requested_by": req.requested_by.display_name if req.requested_by_id else "",
            "decided_by": req.decided_by.display_name if req.decided_by_id else "",
            "revoked_by": req.revoked_by.display_name if req.revoked_by_id else "",
            "tablet": req.device.device_registration_id if req.device_id else "",
            "order_url": reverse("rental-order-detail", args=[order.pk]),
            "list_url": reverse("rental-request-list"),
            "approve_url": reverse("rental-request-approve", args=[req.pk]),
            "reject_url": reverse("rental-request-reject", args=[req.pk]),
            "revoke_url": reverse("rental-request-revoke", args=[req.pk]),
        })
        return context
