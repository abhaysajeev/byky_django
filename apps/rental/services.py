"""Rental Management services."""

import hashlib
import json
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.company.models import PaymentMode
from apps.devices import services as devices_services
from apps.devices.models import BillKind
from apps.discount import services as discount_services
from apps.fare.models import Fare, Package
from apps.fleet.models import Vehicle
from apps.rental.models import (
    MONEY_IN,
    Customer,
    DiscountSource,
    Invoice,
    InvoiceItem,
    Order,
    OrderAction,
    OrderEvent,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    Payment,
    PaymentKind,
    full_number,
)
from core.ids import uuid7


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
    "order_closed": (409, "This order is already completed or cancelled.", False),
    "unknown_item": (404, "That item is not on this order.", False),
    "item_not_active": (409, "That vehicle is already returned, replaced or removed.", False),
    "invalid_return_time": (400, "returned_at is before the vehicle's start time.", False),
    "invalid_time": (400, "That time is before the vehicle's start time.", False),
    "items_still_out": (409, "Every vehicle must be returned before the order is settled.", False),
    "amount_mismatch": (400, "The amounts do not add up.", False),
    "balance_not_settled": (409, "The bill is not paid in full.", False),
    "unknown_card_claim": (404, "That card-discount request is not on this order.", False),
    "card_claim_not_approved": (409, "That card-discount request is not approved.", False),
    "unknown_card_discount": (404, "No card discount with that id.", False),
    # Credit notes (apps/rental/credit_notes.py).
    "order_not_settled": (409, "A credit note needs a settled order.", False),
    "credit_note_pending": (409, "A credit note request is already waiting for this order.", False),
    "credit_note_issued": (409, "This order already has a credit note.", False),
    "unknown_credit_note": (404, "No credit note request with that id.", False),
    "credit_note_closed": (409, "That credit note request is already approved or rejected.", False),
    # Requests (apps/rental/requests.py).
    "discount_request_pending": (409, "A discount request is already waiting for this order.", False),
    "discount_approved": (409, "This order already has an approved discount.", False),
    "card_discount_requested": (409, "This order already has a card-discount request.", False),
    "unknown_request": (404, "No request with that id at this station.", False),
    "request_closed": (409, "That request is already decided or closed.", False),
    # A manager discount at settle.
    "unknown_discount_request": (404, "That discount request is not on this order.", False),
    "discount_request_not_approved": (409, "That discount request is not approved.", False),
    "discount_mismatch": (400, "The discount is not the one approved.", False),
    "approved_discount_not_applied": (409, "This order has an approved discount that must be applied.", False),
    # Complimentary and reprint requests.
    "complimentary_request_pending": (409, "A complimentary request is already waiting for this order.", False),
    "complimentary_approved": (409, "This order is already approved as complimentary.", False),
    "complimentary_requested": (409, "This order already has a complimentary request.", False),
    "discount_requested": (409, "This order already has a manager discount request.", False),
    "reprint_request_pending": (409, "A reprint request is already waiting for this order.", False),
    "reprint_approved": (409, "A reprint is already approved for this order -- print it first.", False),
    "request_not_approved": (409, "That request is not approved.", False),
    "request_used": (409, "That reprint has already been printed.", False),
    "unknown_complimentary_request": (404, "That complimentary request is not on this order.", False),
    "complimentary_request_not_approved": (409, "That complimentary request is not approved.", False),
    "approved_complimentary_not_applied": (409, "This order is approved as complimentary and must settle "
                                                "as complimentary.", False),
}


class OrderRefused(Exception):
    """One of ORDER_ERRORS. `message` overrides the catalogue's when the
    refusal can say which id it was about."""

    def __init__(self, code, message=None, data=None):
        self.status, default, self.retry = ORDER_ERRORS[code]
        self.code, self.message, self.data = code, message or default, data or {}
        super().__init__(self.message)


# Which database constraint means which refusal -- for the race the checks
# before a write cannot close (two tablets, one vehicle, the same instant).
_CONSTRAINT_REFUSALS = {
    "uniq_active_order_item_per_vehicle": "vehicle_already_rented",
    "uniq_order_no_per_company": "order_no_used",
    "order_pkey": "sync_id_conflict",
    "order_item_pkey": "item_id_used",
    "payment_pkey": "payment_id_used",
    "credit_note_pkey": "sync_id_conflict",
    "uniq_live_credit_note_per_order": "credit_note_pending",
    "order_request_pkey": "sync_id_conflict",
    "uniq_live_request_per_order_kind": "discount_request_pending",
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

    No bill is made here: each line keeps its agreed base fare, the line's
    final amount comes at return and the order's bill at settle (design 1
    "Money"). The business checks (blocked customer, available vehicle, ...)
    come later (design 5, decision 7). Ported from: Save_Order_Booking.
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

    refs = _line_refs(company, branch, items_input)
    modes = _payment_modes(company, payments_input)

    order = Order.objects.create(
        id=values["sync_id"], company=company, branch=branch, device=device,
        customer=customer, customer_name=customer.full_name, customer_mobile=customer.mobile_full,
        order_no=values["order_no"], booked_at=values["booked_at"], start_time=values["start_time"],
        is_direct_bill=values.get("is_direct_bill", False),
        is_hotel_order=values.get("is_hotel_order", False), hotel_commission=values.get("hotel_commission") or 0,
        created_by=user, modified_by=user,
    )
    _new_lines(order, items_input, refs, user)
    _record_payments(session, order, payments_input, modes)
    devices_services.raise_counter_from(device, branch, BillKind.ORDER, order.order_no)

    order.refresh_from_db()        # paid_amount, balance_due, payment_status: computed by the database
    event = {
        "action": OrderAction.BOOK, "happened_at": values["booked_at"], "device": device, "user": user,
        "detail": {"items": len(items_input), "advance": str(sum(p["amount"] for p in payments_input))},
    }
    return order, event, reply_for(order)


def _line_refs(company, branch, items_input):
    """(vehicles, fares, offers) by id for new vehicle lines -- or the refusal.
    Each vehicle this company's, at this station, in the call once and not on
    another active line; each fare and offer this company's; each line id new.
    Shared by booking, add and replace."""
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
    # A package (sent as offer_id: the tablet API kept the old name).
    offers = _by_id(Package, company, {item["offer_id"] for item in items_input if item.get("offer_id")},
                    "unknown_offer", "offer")
    if OrderItem.objects.filter(pk__in=[item["sync_id"] for item in items_input]).exists():
        raise OrderRefused("item_id_used")
    return vehicles, fares, offers


def _new_lines(order, items_input, refs, user, replaced_item=None):
    """The new vehicle lines, with the tablet's ids -- the package and its
    agreed base fare only; the rest comes at return (design 1 "Money")."""
    vehicles, fares, offers = refs
    return OrderItem.objects.bulk_create([
        OrderItem(
            id=item["sync_id"], order=order, vehicle=vehicles[item["vehicle_id"]],
            fare=fares.get(item.get("fare_id")), offer=offers.get(item.get("offer_id")),
            package_minutes=item["package_minutes"], start_time=item["start_time"],
            expected_end_time=item["expected_end_time"], base_fare=item["base_fare"],
            replaced_item=replaced_item, created_by=user, modified_by=user,
        )
        for item in items_input
    ])


def _by_id(model, company, ids, code, noun):
    rows = {row.pk: row for row in model.objects.filter(pk__in=ids, company=company)} if ids else {}
    missing = ids - set(rows)
    if missing:
        raise OrderRefused(code, f"No {noun} with id {sorted(missing)[0]}.")
    return rows


# -- Orders: return and settle ------------------------------------------------------


def _open_order(session, order_id):
    """The order, row-locked, at the session's station and still active."""
    order = lock_order(order_id, session.branch)
    if order.status != OrderStatus.ACTIVE:
        raise OrderRefused("order_closed")
    return order


def return_item(session, values, request_data, reply_for):
    """One vehicle back (order_lifecycle_design.md 3.3). Returns (reply, done_now).

    The tablet works out the run time and the amounts (decision 4); the server
    stores them as sent, checking only that total = base fare + overtime. The
    line closes as returned and the vehicle is free to rent at once. Any
    tablet at the order's station may return it (decision 3).
    """

    def apply():
        order = _open_order(session, values["order_id"])
        item = OrderItem.objects.filter(pk=values["item_id"], order=order).first()
        if item is None:
            raise OrderRefused("unknown_item")
        if item.status != OrderItemStatus.ACTIVE:
            raise OrderRefused("item_not_active")
        if values["returned_at"] < item.start_time:
            raise OrderRefused("invalid_return_time")
        if values["total_amount"] != item.base_fare + values["overtime_amount"]:
            raise OrderRefused(
                "amount_mismatch",
                f"total_amount must be base_fare {item.base_fare} + overtime_amount {values['overtime_amount']}.",
            )
        item.status, item.end_time = OrderItemStatus.RETURNED, values["returned_at"]
        item.run_minutes = values["run_minutes"]
        item.overtime_amount, item.total_amount = values["overtime_amount"], values["total_amount"]
        item.modified_by = session.user
        item.save()
        event = {
            "action": OrderAction.RETURN, "item": item, "happened_at": values["returned_at"],
            "device": session.device, "user": session.user,
            "detail": {"run_minutes": item.run_minutes, "overtime_amount": str(item.overtime_amount),
                       "total_amount": str(item.total_amount)},
        }
        return order, event, reply_for(order)

    return run_once(event_id=values["sync_id"], company=session.branch.company, request_data=request_data,
                    apply=apply)


# -- Orders: add, replace, remove a vehicle -------------------------------------------


def add_item(session, values, request_data, reply_for):
    """Another vehicle joins an active order. Returns (reply, done_now).

    The new line goes through the same checks as a booked one. Money taken for
    it now (`payments`, advance only) is recorded in the same call -- never a
    second request.
    """

    def apply():
        order = _open_order(session, values["order_id"])
        item, payments = values["item"], values.get("payments") or []
        refs = _line_refs(order.company, session.branch, [item])
        modes = _payment_modes(order.company, payments)
        [line] = _new_lines(order, [item], refs, session.user)
        _record_payments(session, order, payments, modes)
        order.refresh_from_db()
        event = {
            "action": OrderAction.ADD, "new_item": line, "happened_at": values["added_at"],
            "device": session.device, "user": session.user,
            "detail": {"vehicle_id": line.vehicle_id, "advance": str(sum(p["amount"] for p in payments))},
        }
        return order, event, reply_for(order)

    return run_once(event_id=values["sync_id"], company=session.branch.company, request_data=request_data,
                    apply=apply)


def _active_line(order, item_id, at):
    """The order's line `item_id`, still out, started by `at` -- or the refusal."""
    line = OrderItem.objects.filter(pk=item_id, order=order).first()
    if line is None:
        raise OrderRefused("unknown_item")
    if line.status != OrderItemStatus.ACTIVE:
        raise OrderRefused("item_not_active")
    if at < line.start_time:
        raise OrderRefused("invalid_time")
    return line


def replace_item(session, values, request_data, reply_for):
    """A vehicle swapped for another (order_lifecycle_design.md 3.2). Returns
    (reply, done_now).

    The old line closes as replaced -- with its reason, never billed -- and the
    new one starts, pointing back at it and carrying the package price. The old
    line is closed first, so its vehicle is free before the new line is saved.
    """

    def apply():
        order = _open_order(session, values["order_id"])
        old = _active_line(order, values["old_item_id"], values["replaced_at"])
        new_item = values["new_item"]
        if new_item["vehicle_id"] == old.vehicle_id:
            raise OrderRefused("vehicle_repeated", "The replacement is the same vehicle.")
        old.status, old.end_time, old.reason = OrderItemStatus.REPLACED, values["replaced_at"], values["reason"]
        old.modified_by = session.user
        old.save()
        refs = _line_refs(order.company, session.branch, [new_item])
        [line] = _new_lines(order, [new_item], refs, session.user, replaced_item=old)
        event = {
            "action": OrderAction.REPLACE, "item": old, "new_item": line, "happened_at": values["replaced_at"],
            "device": session.device, "user": session.user,
            "detail": {"reason": values["reason"], "old_vehicle_id": old.vehicle_id,
                       "new_vehicle_id": line.vehicle_id},
        }
        return order, event, reply_for(order)

    return run_once(event_id=values["sync_id"], company=session.branch.company, request_data=request_data,
                    apply=apply)


def remove_item(session, values, request_data, reply_for):
    """A vehicle taken off an order with no replacement. Returns (reply,
    done_now). The line stays, as removed with its reason -- never billed --
    and the vehicle is free at once. A vehicle the customer rode is returned,
    not removed."""

    def apply():
        order = _open_order(session, values["order_id"])
        line = _active_line(order, values["item_id"], values["removed_at"])
        line.status, line.end_time, line.reason = OrderItemStatus.REMOVED, values["removed_at"], values["reason"]
        line.modified_by = session.user
        line.save()
        event = {
            "action": OrderAction.REMOVE, "item": line, "happened_at": values["removed_at"],
            "device": session.device, "user": session.user,
            "detail": {"reason": values["reason"], "vehicle_id": line.vehicle_id},
        }
        return order, event, reply_for(order)

    return run_once(event_id=values["sync_id"], company=session.branch.company, request_data=request_data,
                    apply=apply)


# -- Orders: standalone payments ---------------------------------------------------------

# Which kind of money may move on an order in each state: an advance only
# while it is out, a refund also once it is cancelled; nothing once settled --
# its bill is invoiced.
_PAYMENT_ALLOWED = {
    PaymentKind.ADVANCE: (OrderStatus.ACTIVE,),
    PaymentKind.REFUND: (OrderStatus.ACTIVE, OrderStatus.CANCELLED),
}


def record_payments(session, values, request_data, reply_for):
    """Money moving on its own -- an extra advance, or a refund (decision 2).
    Returns (reply, done_now).

    One kind for the whole call, each entry through record_payment. Money that
    belongs to a booking, an added vehicle or a settlement is sent with that
    call instead -- each payment once. Whether a refund exceeds what was paid
    is a later validation (decision 7).
    """

    def apply():
        order = lock_order(values["order_id"], session.branch)
        kind = values["kind"]
        if order.status not in _PAYMENT_ALLOWED[kind]:
            raise OrderRefused("order_closed", f"No {kind} can be taken on a {order.status} order.")
        entries = [{**payment, "kind": kind} for payment in values["payments"]]
        _record_payments(session, order, entries, _payment_modes(order.company, entries))
        order.refresh_from_db()
        event = {
            "action": OrderAction.PAYMENT, "happened_at": entries[0]["paid_at"],
            "device": session.device, "user": session.user,
            "detail": {"kind": kind, "total": str(sum(p["amount"] for p in entries)), "count": len(entries)},
        }
        return order, event, reply_for(order)

    return run_once(event_id=values["sync_id"], company=session.branch.company, request_data=request_data,
                    apply=apply)


def settle_order(session, values, request_data, reply_for):
    """Close the bill (order_lifecycle_design.md 3.4). Returns (reply, done_now).

    Every vehicle must already be back. The tablet works out the whole bill
    and sends it; the server stores it as sent after the basic checks -- the
    subtotal is the returned lines' totals, the net adds up, and after this
    call's payments the bill is paid in full (so `paid` is always true).

    VAT is included in the fares (client, 3 Oct 2026): net_amount = subtotal
    - discount_amount + rounding_adjustment, nothing added for VAT.
    tax_percentage / tax_amount are the VAT *contained* in the net -- stored
    as sent and shown on the tax invoice, never part of the sum.

    The one discount is a card discount (`card_discount`), a manager's
    approved discount (`manager_discount`) or an approved complimentary ride
    (`complimentary`: the whole subtotal off, net 0, any advance refunded in
    `payments`) -- the last two checked against the approval
    (apps/rental/requests.py::check_at_settle), and an approval must be
    applied. In one transaction: the card discount redeemed (and unused card
    requests cancelled), the approval marked applied (and waiting requests
    closed), the payments recorded, the order completed and the invoice
    issued. A zero bill settles too.
    """
    from apps.rental import requests as order_requests  # it imports this module

    def apply():
        order = _open_order(session, values["order_id"])
        items = list(order.items.select_related("vehicle__vehicle_type"))
        if any(item.status == OrderItemStatus.ACTIVE for item in items):
            raise OrderRefused("items_still_out")

        billed = [item for item in items if item.status == OrderItemStatus.RETURNED]
        card, manager = values.get("card_discount"), values.get("manager_discount")
        free = values.get("complimentary")
        discount = card or manager
        # A complimentary ride: the whole subtotal is the discount, net 0.
        discount_amount = (values["subtotal"] if free else
                           discount["discount_amount"] if discount else Decimal("0"))
        lines_total = sum((item.total_amount for item in billed), Decimal("0"))
        if values["subtotal"] != lines_total:
            raise OrderRefused("amount_mismatch", f"subtotal must be the returned vehicles' total, {lines_total}.")
        # VAT is inside the prices: it is not added here.
        expected_net = values["subtotal"] - discount_amount + values["rounding_adjustment"]
        if values["net_amount"] != expected_net:
            raise OrderRefused(
                "amount_mismatch",
                f"net_amount must be subtotal - discount_amount + rounding_adjustment = {expected_net} "
                f"(VAT is included in the fares, not added).",
            )

        approval = order_requests.check_at_settle(order, values)
        claim = None
        if card:
            try:
                claim = discount_services.redeem_for_order(
                    session, order, card, at=values["settled_at"],
                    bill_amount=values["subtotal"], net_amount=values["net_amount"],
                )
            except discount_services.RedeemRefused as refused:
                raise OrderRefused(refused.code) from None
        else:
            discount_services.cancel_unused_for_order(order, now=values["settled_at"])

        payments = values.get("payments") or []
        _record_payments(session, order, payments, _payment_modes(order.company, payments))

        order.refresh_from_db()
        if order.paid_amount != values["net_amount"]:
            raise OrderRefused(
                "balance_not_settled",
                f"Paid {order.paid_amount} against a bill of {values['net_amount']}: the bill is not paid in full.",
            )

        order.subtotal, order.tax_percentage = values["subtotal"], values["tax_percentage"]
        order.tax_amount, order.rounding_adjustment = values["tax_amount"], values["rounding_adjustment"]
        order.net_amount = values["net_amount"]
        order_requests.close_at_settle(order, approval, discount_amount, values["settled_at"], session.user)

        order.discount_claim = claim
        order.discount_percentage = (Decimal("100") if free else
                                     discount.get("discount_percentage") if discount else None)
        order.discount_amount = discount_amount
        order.status, order.completed_at = OrderStatus.COMPLETED, values["settled_at"]
        order.modified_by = session.user
        order.save()
        order.refresh_from_db()            # payment_status, balance_due: computed by the database

        source = (DiscountSource.CARD if card else DiscountSource.MANAGER if manager
                  else DiscountSource.COMPLIMENTARY if free else "")
        issue_invoice(order, billed, session, at=values["settled_at"], discount_source=source)
        event = {
            "action": OrderAction.SETTLE, "happened_at": values["settled_at"],
            "device": session.device, "user": session.user,
            "detail": {"net_amount": str(order.net_amount), "discount_amount": str(order.discount_amount),
                       "payments": len(payments)},
        }
        return order, event, reply_for(order)

    return run_once(event_id=values["sync_id"], company=session.branch.company, request_data=request_data,
                    apply=apply)


def _payment_modes(company, payments):
    """{id: PaymentMode} for a call's payment entries -- each mode this
    company's and active, each entry id unused -- or the refusal."""
    if not payments:
        return {}
    modes = {
        mode.pk: mode for mode in PaymentMode.objects.filter(
            pk__in={p["payment_mode_id"] for p in payments}, company=company, is_active=True,
        )
    }
    for payment in payments:
        if payment["payment_mode_id"] not in modes:
            raise OrderRefused("unknown_payment_mode", f"No payment mode with id {payment['payment_mode_id']}.")
    if Payment.objects.filter(pk__in=[p["sync_id"] for p in payments]).exists():
        raise OrderRefused("payment_id_used")
    return modes


def _record_payments(session, order, payments, modes):
    """Each payment entry of a call, through record_payment."""
    for payment in payments:
        record_payment(
            order, payment_id=payment["sync_id"], kind=payment["kind"], mode=modes[payment["payment_mode_id"]],
            amount=payment["amount"], paid_at=payment["paid_at"], device=session.device, user=session.user,
            reference_no=payment.get("reference_no") or "", reference_date=payment.get("reference_date"),
        )


def issue_invoice(order, billed_items, session, *, at, discount_source=""):
    """The invoice for a settled order: a frozen copy of the bill, numbered by
    the order, issued by the settling tablet and operator. Only returned lines
    are billed -- replaced and removed ones are not copied."""
    company, branch = order.company, order.branch
    invoice = Invoice.objects.create(
        id=uuid7(), order=order, company=company, branch=branch, device=session.device, issued_by=session.user,
        invoice_no=order.order_no, issued_at=at,
        company_name=company.name, company_trn=company.income_tax_number or "", branch_name=branch.name,
        customer_name=order.customer_name, customer_mobile=order.customer_mobile,
        subtotal=order.subtotal, discount_percentage=order.discount_percentage,
        discount_amount=order.discount_amount, discount_source=discount_source,
        tax_percentage=order.tax_percentage, tax_amount=order.tax_amount,
        rounding_adjustment=order.rounding_adjustment, net_amount=order.net_amount,
        payments=[
            {"mode": payment.mode.name, "kind": payment.kind, "amount": str(payment.amount),
             "reference_no": payment.reference_no}
            for payment in order.payments.select_related("mode").order_by("paid_at", "created_on")
        ],
        created_by=session.user, modified_by=session.user,
    )
    InvoiceItem.objects.bulk_create([
        InvoiceItem(
            id=uuid7(), invoice=invoice, order_item=item,
            vehicle_identifier=item.vehicle.identifier, vehicle_name=item.vehicle.vehicle_name,
            vehicle_type=item.vehicle.vehicle_type.vehicle_type_name,
            package_minutes=item.package_minutes, run_minutes=item.run_minutes,
            start_time=item.start_time, end_time=item.end_time, base_fare=item.base_fare,
            overtime_amount=item.overtime_amount, total_amount=item.total_amount,
        )
        for item in sorted(billed_items, key=lambda line: line.start_time)
    ])
    return invoice


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
