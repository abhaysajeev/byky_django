"""Rental Management services."""

import hashlib
import json

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.company.models import PaymentMode
from apps.devices import services as devices_services
from apps.devices.models import BillKind
from apps.fare.models import Fare, Offer
from apps.fleet.models import Vehicle
from apps.rental.models import (
    MONEY_IN,
    Customer,
    Order,
    OrderAction,
    OrderEvent,
    OrderItem,
    OrderItemStatus,
    Payment,
    full_number,
)


def next_customer_code(company):
    """CU001, CU002, ... -- the next unused code for this company.

    Not exposed on the Add form: the wireframe's own Add form has no
    Customer Code field, only its list column does, so it's generated here.
    """
    last = (
        Customer.objects.filter(company=company, customer_code__startswith="CU")
        .order_by("-customer_code")
        .values_list("customer_code", flat=True)
        .first()
    )
    next_number = int(last[2:]) + 1 if last and last[2:].isdigit() else 1
    code = f"CU{next_number:03d}"
    while Customer.objects.filter(company=company, customer_code=code).exists():
        next_number += 1
        code = f"CU{next_number:03d}"
    return code


class UnknownCustomer(Exception):
    pass


def customer_by_phone(company, full_number):
    """The device lookup, by the full number (country code + number, digits
    only) -- one index hit. Filtered by company too, even though the full
    number is globally unique, as a defense-in-depth company boundary: a
    device at one company never fetches another company's customer, even by
    guessing a phone number that happens to be theirs."""
    customer = Customer.objects.filter(company=company, mobile_full=full_number).first()
    if customer is None:
        raise UnknownCustomer()
    return customer


def record_payment(order, *, payment_id, kind, mode, amount, paid_at, device=None, user=None,
                   reference_no="", reference_date=None, remarks=""):
    """One payment entry on an order -- the only way one is written.

    Adds the entry and moves the order's running total in the same
    transaction: amount_received for money in (advance, settlement),
    amount_refunded for money out (refund). The increment happens in the
    database (F), so two payments on one order at the same moment cannot
    miscount. paid_amount, balance_due and payment_status follow on their
    own (order_lifecycle_design.md 1 "Money", 4.4).

    Entries are never edited or deleted; a mistake is a reversing refund.
    """
    total = "amount_received" if kind in MONEY_IN else "amount_refunded"
    with transaction.atomic():
        payment = Payment.objects.create(
            id=payment_id, order=order, kind=kind, mode=mode, amount=amount, paid_at=paid_at,
            reference_no=reference_no, reference_date=reference_date, remarks=remarks,
            device=device, collected_by=user, created_by=user, modified_by=user,
        )
        Order.objects.filter(pk=order.pk).update(
            **{total: F(total) + amount}, modified_by=user, modified_on=timezone.now(),
        )
    return payment


# -- Orders: refusals ---------------------------------------------------------
#
# Every code an order call can answer with, once: HTTP status, message, and
# whether the tablet should keep the call queued and retry (True) or stop and
# show the operator (False) -- order_lifecycle_design.md 2 "Reliability rules".
# The views build their Swagger rows and table from this, so the docs cannot
# drift from what is sent.

ORDER_ERRORS = {
    "order_not_synced": (409, "That order has not reached the server yet.", True),
    "sync_id_conflict": (409, "That sync_id is already used for something else.", False),
    "order_no_used": (409, "That order number is already used.", False),
    "vehicle_already_rented": (409, "A vehicle in this order is already on an active rental.", False),
    "vehicle_repeated": (400, "The same vehicle is in this order twice.", False),
    "item_id_used": (409, "An item sync_id is already used.", False),
    "payment_id_used": (409, "A payment sync_id is already used.", False),
    "unknown_customer": (400, "No customer with that id.", False),
    "unknown_payment_mode": (400, "No payment mode with that id.", False),
    "unknown_vehicle": (400, "No vehicle with that id.", False),
    "vehicle_not_at_station": (400, "That vehicle is not at this station.", False),
    "unknown_fare": (400, "No fare with that id.", False),
    "unknown_offer": (400, "No offer with that id.", False),
    "unknown_order": (404, "No order with that id at this station.", False),
}


class OrderRefused(Exception):
    """One of ORDER_ERRORS. `message` overrides the catalogue's when the
    refusal can say which id it was about."""

    def __init__(self, code, message=None):
        self.status, default, self.retry = ORDER_ERRORS[code]
        self.code, self.message = code, message or default
        super().__init__(self.message)


# Which database constraint means which refusal -- for the race the checks
# before a write cannot close (two tablets, one vehicle, the same instant).
_CONSTRAINT_REFUSALS = {
    "uniq_active_order_item_per_vehicle": "vehicle_already_rented",
    "uniq_order_no_per_company": "order_no_used",
    "order_pkey": "sync_id_conflict",
    "order_item_pkey": "item_id_used",
    "payment_pkey": "payment_id_used",
}


def _refusal_for(error):
    diag = getattr(error.__cause__, "diag", None)
    code = _CONSTRAINT_REFUSALS.get(getattr(diag, "constraint_name", None))
    return OrderRefused(code) if code else None


# -- Orders: one call, once ------------------------------------------------------


def request_hash(request_data):
    """SHA-256 of the call's request_data, keys sorted -- the same body sent
    with its keys in another order is the same call."""
    body = json.dumps(request_data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def _stored_reply(event_id, company, fingerprint):
    """The reply already given to call `event_id`, or None if there is no
    such call. The same id with another body, or another company's, is a
    conflict -- never silently one version or the other."""
    event = OrderEvent.objects.filter(pk=event_id).select_related("order").first()
    if event is None:
        return None
    if event.order.company_id != company.pk or event.request_hash != fingerprint:
        raise OrderRefused("sync_id_conflict")
    return event.response


def run_once(*, event_id, company, request_data, apply):
    """Run one order call exactly once. Returns (reply data, done_now).

    A call is identified by its own sync_id (`event_id`). Already stored with
    the same body: its stored reply, done_now False (the app's `duplicate`),
    nothing written. Stored with a different body: sync_id_conflict.
    Otherwise `apply()` runs inside one transaction and returns
    (order, event fields, reply data); the OrderEvent holding that reply is
    written in the same transaction, so a call is either wholly done and
    recorded or not done at all (order_lifecycle_design.md 2).

    Two identical calls at once: the second one's insert waits on the
    first's, fails, and finds the first's event -- answered duplicate. Any
    other constraint failure becomes its own refusal (_CONSTRAINT_REFUSALS).
    """
    fingerprint = request_hash(request_data)
    stored = _stored_reply(event_id, company, fingerprint)
    if stored is not None:
        return stored, False
    try:
        with transaction.atomic():
            order, event, reply = apply()
            OrderEvent.objects.create(
                id=event_id, order=order, request_hash=fingerprint, response=reply, **event,
            )
    except IntegrityError as error:
        stored = _stored_reply(event_id, company, fingerprint)
        if stored is not None:
            return stored, False
        refusal = _refusal_for(error)
        if refusal is None:
            raise
        raise refusal from None
    return reply, True


def lock_order(order_id, branch):
    """The order `order_id` at `branch`, row-locked until the transaction
    ends -- so two tablets acting on one order take turns. Every change after
    booking goes through it. An order the server has not got yet is
    order_not_synced (retry: the booking is still in the tablet's queue)."""
    order = Order.objects.select_for_update().filter(pk=order_id, branch=branch).first()
    if order is None:
        raise OrderRefused("order_not_synced")
    return order


def order_at_station(branch, *, sync_id=None, order_no=None):
    """One order at `branch`, by its sync_id or its order number -- any
    tablet at the order's station may read it (design 5, decision 3). An
    order elsewhere is unknown_order, the same as one that does not exist."""
    orders = Order.objects.filter(branch=branch)
    order = (orders.filter(pk=sync_id) if sync_id else orders.filter(order_no=order_no)).first()
    if order is None:
        raise OrderRefused("unknown_order")
    return order


def active_rentals(vehicle_ids):
    """{vehicle_id: (order_no, expected_end_time)} for the vehicles in
    `vehicle_ids` that are out on rent now -- on an active order item (one
    per vehicle, enforced by the database). "On rent" is never stored
    (design 5, decision 6)."""
    rows = OrderItem.objects.filter(vehicle_id__in=vehicle_ids, status=OrderItemStatus.ACTIVE).values_list(
        "vehicle_id", "order__order_no", "expected_end_time",
    )
    return {vehicle_id: (order_no, end) for vehicle_id, order_no, end in rows}


# -- Orders: booking -------------------------------------------------------------


def create_rental_order(session, values, request_data, reply_for):
    """One rental booking -- the order, its vehicle lines and any advance
    payments, in one call. Returns (reply data, created); created is False for
    a resend of a call already done, which the app treats as success.

    `session` is the AppSession (apps/portal/session_models.py) the request
    authenticated as -- branch, device and user all come from it, never from
    `values`. `values` is the booking serializer's validated data, every
    datetime already made timezone-aware by the view; `request_data` is the
    raw body, fingerprinted by run_once. `reply_for(order)` builds the reply,
    which is stored with the call and replayed on a resend.

    The order's sync_id is also the call's: a booking happens once per order.
    The tablet makes every id -- order, lines, payments -- so it can act on
    them before the server has replied (design 2).

    Money figures are trusted as sent, not recomputed; the business checks
    (totals adding up, blocked customer, available vehicle, ...) come later
    (design 5, decision 7). Ported from: Save_Order_Booking.
    """
    sync_id = values["sync_id"]
    try:
        if not OrderEvent.objects.filter(pk=sync_id).exists() and Order.objects.filter(pk=sync_id).exists():
            # An order stored before order events existed: not this call.
            raise OrderRefused("sync_id_conflict")
        return run_once(
            event_id=sync_id, company=session.branch.company, request_data=request_data,
            apply=lambda: _book(session, values, reply_for),
        )
    except OrderRefused:
        count_receipt(session, values["order_no"])
        raise


def count_receipt(session, order_no):
    """Raise the tablet's receipt counter for a booking that was refused.

    The receipt was printed and handed over before the upload, so its number
    is used whether or not the server keeps the order. Counted outside the
    refused booking's transaction, which rolled back; otherwise a tablet
    reinstalled afterwards would be handed that number again at login.
    """
    if isinstance(order_no, str) and order_no:
        devices_services.raise_counter_from(session.device, session.branch, BillKind.ORDER, order_no)


def _book(session, values, reply_for):
    branch, user, device = session.branch, session.user, session.device
    company = branch.company
    items_input, payments_input = values["items"], values.get("payments") or []

    if Order.objects.filter(company=company, order_no=values["order_no"]).exists():
        raise OrderRefused("order_no_used")

    customer = Customer.objects.filter(pk=values["customer_id"], company=company).first()
    if customer is None:
        raise OrderRefused("unknown_customer")

    vehicle_list = [item["vehicle_id"] for item in items_input]
    if len(set(vehicle_list)) != len(vehicle_list):
        raise OrderRefused("vehicle_repeated")
    vehicles = {v.pk: v for v in Vehicle.objects.filter(pk__in=vehicle_list, company=company)}
    missing = set(vehicle_list) - set(vehicles)
    if missing:
        raise OrderRefused("unknown_vehicle", f"No vehicle with id {sorted(missing)[0]}.")
    off_station = sorted(v.pk for v in vehicles.values() if v.current_branch_id != branch.pk)
    if off_station:
        raise OrderRefused("vehicle_not_at_station", f"Vehicle {off_station[0]} is not at this station.")
    rented = active_rentals(vehicle_list)
    if rented:
        raise OrderRefused("vehicle_already_rented", f"Vehicle {min(rented)} is already on an active rental.")

    fares = _by_id(Fare, company, {item["fare_id"] for item in items_input if item.get("fare_id")},
                   "unknown_fare", "fare")
    offers = _by_id(Offer, company, {item["offer_id"] for item in items_input if item.get("offer_id")},
                    "unknown_offer", "offer")
    modes = {
        mode.pk: mode for mode in PaymentMode.objects.filter(
            pk__in={p["payment_mode_id"] for p in payments_input}, company=company, is_active=True,
        )
    }
    for payment in payments_input:
        if payment["payment_mode_id"] not in modes:
            raise OrderRefused("unknown_payment_mode", f"No payment mode with id {payment['payment_mode_id']}.")

    if OrderItem.objects.filter(pk__in=[item["sync_id"] for item in items_input]).exists():
        raise OrderRefused("item_id_used")
    if Payment.objects.filter(pk__in=[p["sync_id"] for p in payments_input]).exists():
        raise OrderRefused("payment_id_used")

    order = Order.objects.create(
        id=values["sync_id"], company=company, branch=branch, device=device,
        customer=customer, customer_name=customer.full_name, customer_mobile=customer.mobile_full,
        order_no=values["order_no"], booked_at=values["booked_at"], start_time=values["start_time"],
        total_amount=values["total_amount"], total_discount=values.get("total_discount") or 0,
        total_tax=values.get("total_tax") or 0, tax_percentage=values.get("tax_percentage") or 0,
        rounded_diff=values.get("rounded_diff") or 0, net_amount=values["net_amount"],
        is_direct_bill=values.get("is_direct_bill", False),
        is_hotel_order=values.get("is_hotel_order", False), hotel_commission=values.get("hotel_commission") or 0,
        created_by=user, modified_by=user,
    )
    OrderItem.objects.bulk_create([
        OrderItem(
            id=item["sync_id"], order=order, vehicle=vehicles[item["vehicle_id"]],
            fare=fares.get(item.get("fare_id")), offer=offers.get(item.get("offer_id")),
            package_minutes=item["package_minutes"], start_time=item["start_time"],
            expected_end_time=item["expected_end_time"], rate=item["rate"], amount=item["amount"],
            discount=item.get("discount") or 0, tax_amount=item.get("tax_amount") or 0,
            total_amount=item["total_amount"], created_by=user, modified_by=user,
        )
        for item in items_input
    ])
    for payment in payments_input:
        record_payment(
            order, payment_id=payment["sync_id"], kind=payment["kind"], mode=modes[payment["payment_mode_id"]],
            amount=payment["amount"], paid_at=payment["paid_at"], device=device, user=user,
            reference_no=payment.get("reference_no") or "", reference_date=payment.get("reference_date"),
        )
    devices_services.raise_counter_from(device, branch, BillKind.ORDER, order.order_no)

    order.refresh_from_db()        # paid_amount, balance_due, payment_status: computed by the database
    event = {
        "action": OrderAction.BOOK, "happened_at": values["booked_at"], "device": device, "user": user,
        "detail": {"items": len(items_input), "advance": str(sum(p["amount"] for p in payments_input))},
    }
    return order, event, reply_for(order)


def _by_id(model, company, ids, code, noun):
    rows = {row.pk: row for row in model.objects.filter(pk__in=ids, company=company)} if ids else {}
    missing = ids - set(rows)
    if missing:
        raise OrderRefused(code, f"No {noun} with id {sorted(missing)[0]}.")
    return rows


class CustomerExists(Exception):
    """The phone is already registered. `customer` is the existing one when it
    belongs to the caller's company, None when it belongs to another -- whose
    details are never handed across."""

    def __init__(self, customer):
        super().__init__("customer_exists")
        self.customer = customer


class SyncIdConflict(Exception):
    """This sync_id is already another company's customer."""


# What a device may set on a new customer. Blocking, the code and the
# document upload stay with the web screen.
_DEVICE_FIELDS = (
    "first_name", "last_name", "gender", "date_of_birth", "nationality", "id_type", "id_no",
    "mobile_country_code", "mobile_no", "email", "address", "remarks",
)


def create_customer(user, company, values):
    """A customer registered by the operator app. Returns (customer, created):
    created is False when this sync_id was already stored, which the app
    treats as success -- an offline create resent is stored once.

    Raises CustomerExists or SyncIdConflict; nothing is written then.
    """
    sync_id = values["sync_id"]
    existing = _by_sync_id(company, sync_id)
    if existing is not None:
        return existing, False
    _refuse_taken_phone(company, full_number(values["mobile_country_code"], values["mobile_no"]))

    customer = Customer(company=company, sync_id=sync_id, created_by=user, modified_by=user,
                        **{field: values[field] for field in _DEVICE_FIELDS if field in values})
    customer.apply_approval_defaults()
    try:
        with transaction.atomic():
            customer.customer_code = next_customer_code(company)
            customer.save(force_insert=True)
    except IntegrityError:
        # Lost a race: the same sync_id or the same phone arrived at once.
        existing = _by_sync_id(company, sync_id)
        if existing is not None:
            return existing, False
        _refuse_taken_phone(company, customer.mobile_full)
        raise
    return customer, True


def _by_sync_id(company, sync_id):
    existing = Customer.objects.filter(sync_id=sync_id).first()
    if existing is not None and existing.company_id != company.pk:
        raise SyncIdConflict()
    return existing


def _refuse_taken_phone(company, mobile_full):
    taken = Customer.objects.filter(mobile_full=mobile_full).first()
    if taken is not None:
        raise CustomerExists(taken if taken.company_id == company.pk else None)
