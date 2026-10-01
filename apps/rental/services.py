"""Rental Management services."""

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.company.models import PaymentMode
from apps.fare.models import Fare, Offer
from apps.fleet.models import Vehicle
from apps.rental.models import Customer, Order, OrderItem, Payment, PaymentKind, full_number
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


class OrderRefused(Exception):
    """code/message/status for the envelope, same shape as
    apps.crew.services.AttendanceRefused."""

    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def create_rental_order(session, values):
    """One rental booking -- order header, its vehicle lines, and (if money
    was collected) the first payment, in one call. Returns (order, created);
    created is False when sync_id was already stored, which the app treats
    as success (apps.crew.services.mark_attendance is the same contract).

    `session` is the AppSession (apps/portal/session_models.py) the request
    authenticated as -- branch, device and user all come from it, never from
    `values`. `values` is the create-order serializer's validated data, with
    every datetime already made timezone-aware by the view.

    Device-sent money figures (rates, totals, tax, discount) are trusted as
    sent, not recomputed against apps.fare.pricing -- a deliberate decision,
    not an oversight (design discussion).
    """
    branch = session.branch
    if branch is None:
        raise OrderRefused("device_not_mapped", "This device has no station.", 409)
    if not branch.is_active:
        raise OrderRefused("branch_inactive", "This station is closed.", 409)
    company = branch.company
    sync_id = values["sync_id"]

    existing = Order.objects.filter(pk=sync_id).first()
    if existing is not None:
        if existing.company_id != company.pk:
            raise OrderRefused("sync_id_conflict", "That sync_id is already used.", 409)
        return existing, False

    if Order.objects.filter(company=company, order_no=values["order_no"]).exists():
        raise OrderRefused("order_no_used", "That order number is already used.", 409)

    customer = Customer.objects.filter(pk=values["customer_id"], company=company).first()
    if customer is None:
        raise OrderRefused("unknown_customer", "No customer with that id.")

    payment_mode = PaymentMode.objects.filter(
        pk=values["payment_mode_id"], company=company, is_active=True,
    ).first()
    if payment_mode is None:
        raise OrderRefused("unknown_payment_mode", "No payment mode with that id.")

    items_input = values["items"]
    vehicle_ids = {item["vehicle_id"] for item in items_input}
    vehicles = {v.pk: v for v in Vehicle.objects.filter(pk__in=vehicle_ids, company=company)}
    missing = vehicle_ids - set(vehicles)
    if missing:
        raise OrderRefused("unknown_vehicle", f"No vehicle with id {sorted(missing)[0]}.")
    off_station = [v.pk for v in vehicles.values() if v.current_branch_id != branch.pk]
    if off_station:
        raise OrderRefused("vehicle_not_at_station", f"Vehicle {off_station[0]} is not at this station.")

    fare_ids = {item["fare_id"] for item in items_input if item.get("fare_id")}
    fares = {f.pk: f for f in Fare.objects.filter(pk__in=fare_ids, company=company)} if fare_ids else {}
    missing_fares = fare_ids - set(fares)
    if missing_fares:
        raise OrderRefused("unknown_fare", f"No fare with id {sorted(missing_fares)[0]}.")

    offer_ids = {item["offer_id"] for item in items_input if item.get("offer_id")}
    offers = {o.pk: o for o in Offer.objects.filter(pk__in=offer_ids, company=company)} if offer_ids else {}
    missing_offers = offer_ids - set(offers)
    if missing_offers:
        raise OrderRefused("unknown_offer", f"No offer with id {sorted(missing_offers)[0]}.")

    synced_at = timezone.now()
    order = Order(
        id=sync_id, company=company, branch=branch, device=session.device,
        customer=customer, customer_name=customer.full_name, customer_mobile=customer.mobile_full,
        order_no=values["order_no"], booked_at=values["device_created_at"], start_time=values["start_time"],
        total_amount=values["total_amount"], total_discount=values.get("total_discount") or 0,
        total_tax=values.get("total_tax") or 0, tax_percentage=values.get("tax_percentage") or 0,
        rounded_diff=values.get("rounded_diff") or 0, net_amount=values["net_amount"],
        is_direct_bill=values.get("is_direct_bill", False),
        is_hotel_order=values.get("is_hotel_order", False), hotel_commission=values.get("hotel_commission") or 0,
        created_by=session.user, modified_by=session.user,
    )
    item_rows = [
        OrderItem(
            id=uuid7(), order_id=sync_id, vehicle=vehicles[item["vehicle_id"]],
            fare=fares.get(item.get("fare_id")), offer=offers.get(item.get("offer_id")),
            package_minutes=item["package_minutes"], start_time=item["start_time"],
            expected_end_time=item["expected_end_time"], rate=item["rate"], amount=item["amount"],
            discount=item.get("discount") or 0, tax_amount=item.get("tax_amount") or 0,
            total_amount=item["total_amount"], reason=item.get("remarks") or "",
            created_by=session.user, modified_by=session.user,
        )
        for item in items_input
    ]
    collected = values.get("collected_amount") or 0

    try:
        with transaction.atomic():
            order.save(force_insert=True)
            OrderItem.objects.bulk_create(item_rows)
            if collected > 0:
                Payment.objects.create(
                    order=order, kind=PaymentKind.ADVANCE, mode=payment_mode, amount=collected,
                    device=session.device, collected_by=session.user, collected_at=synced_at,
                    created_by=session.user, modified_by=session.user,
                )
                order.amount_received = collected
                order.save(update_fields=["amount_received"])
    except IntegrityError:
        # Either a concurrent call with the same sync_id already won (the
        # order row exists -- answer duplicate, the same race
        # mark_attendance handles), or a vehicle in this order was claimed by
        # another order in between the check above and this insert (the
        # order row is gone too, this whole block having rolled back).
        raced = Order.objects.filter(pk=sync_id).first()
        if raced is not None:
            return raced, False
        raise OrderRefused(
            "vehicle_already_rented", "A vehicle in this order is already on an active rental.", 409,
        ) from None

    order.refresh_from_db()        # paid_amount, balance_due, payment_status: computed by the database
    return order, True


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
