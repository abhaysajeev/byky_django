"""Rental Management services."""

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.company.models import PaymentMode
from apps.fare.models import Fare, Offer
from apps.fleet.models import Vehicle
from apps.rental.models import Customer, Order, OrderItem, Payment, PaymentKind


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


def customer_by_phone(company, mobile_no):
    """The device lookup: filtered by company too, even though mobile_no is
    globally unique, as a defense-in-depth company boundary -- a device at
    one company should never be able to fetch another company's customer,
    even by guessing a phone number that happens to be theirs."""
    customer = Customer.objects.filter(company=company, mobile_no=mobile_no).first()
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
        customer=customer, customer_name=customer.full_name, customer_mobile=customer.mobile_no,
        order_no=values.get("order_no", ""),
        device_created_at=values["device_created_at"], synced_at=synced_at,
        start_time=values["start_time"], number_of_vehicles=len(items_input),
        total_amount=values["total_amount"], total_discount=values.get("total_discount") or 0,
        total_tax=values.get("total_tax") or 0, tax_percentage=values.get("tax_percentage") or 0,
        rounded_diff=values.get("rounded_diff") or 0, net_amount=values["net_amount"],
        advance_amount=values.get("advance_amount") or 0, payment_mode=payment_mode,
        is_direct_bill=values.get("is_direct_bill", False),
        is_hotel_order=values.get("is_hotel_order", False), hotel_commission=values.get("hotel_commission") or 0,
        created_by=session.user, modified_by=session.user,
    )
    item_rows = [
        OrderItem(
            order_id=sync_id, vehicle=vehicles[item["vehicle_id"]],
            fare=fares.get(item.get("fare_id")), offer=offers.get(item.get("offer_id")),
            package_minutes=item["package_minutes"], start_time=item["start_time"],
            expected_end_time=item["expected_end_time"], rate=item["rate"], amount=item["amount"],
            discount=item.get("discount") or 0, tax_amount=item.get("tax_amount") or 0,
            total_amount=item["total_amount"], remarks=item.get("remarks") or "",
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
                order.paid_amount = collected
                order.save(update_fields=["paid_amount"])
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

    return order, True
