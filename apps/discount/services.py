"""Discount Card rules. Views only translate HTTP.

save_card_discount() -- the Card Discount page's one post: the discount and
its day rows, checked together and written in one transaction.
decide_claim() -- an approver's Approve / Reject on a pending claim.
card_usage() -- a customer's redemptions of the discounts usable today (device API).
request_approval() / cancel_pending_for_order() -- the approval request's life.
redeem_for_order() / cancel_unused_for_order() -- the order's settlement.
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import F, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.company.models import WeekDay
from apps.company.scoping import companies_for
from apps.discount.models import (
    CardDiscount,
    CardDiscountClaim,
    CardDiscountDay,
    ClaimStatus,
    FareBasis,
    UsageType,
)
from apps.discount.scoping import card_discounts_for, card_grades_for
from apps.rental.models import REQUEST_LIVE, Customer, Order, OrderRequest, OrderRequestKind
from core.ids import uuid7
from core.timezones import business_date_for, zone_for

# The four options the screen shows (legacy RMSCardPromotionType), stored as
# two fields: (value, label, requires_approval, fare_basis).
PROMOTIONS = [
    ("auto_base", "Automatic – Base Fare", False, FareBasis.BASE),
    ("auto_full", "Automatic – Full Fare", False, FareBasis.FULL),
    ("approval_base", "Approval – Base Fare", True, FareBasis.BASE),
    ("approval_full", "Approval – Full Fare", True, FareBasis.FULL),
]
_PROMOTION = {value: (approval, basis) for value, _, approval, basis in PROMOTIONS}

OVERLAP = "Another active discount covers these dates for this grade."
MAX_LIMIT = 32767


class Invalid(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


class NotFound(Exception):
    pass


def promotion_of(discount):
    for value, _, approval, basis in PROMOTIONS:
        if discount.requires_approval == approval and discount.fare_basis == basis:
            return value
    return ""


def _date(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _percent(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0 or number > 100 or number != number.quantize(Decimal("0.01")):
        return None
    return number


def _parse(user, data, errors):
    def error(field, message):
        errors.append({"field": field, "message": message})

    if user.sees_every_company:
        company = companies_for(user).filter(pk=data.get("company") or 0).first()
        if company is None:
            error("Company", "Choose a company.")
    else:
        company = user.company

    grade = card_grades_for(user).select_related("card_type").filter(pk=data.get("card_grade") or 0).first()
    if grade is None:
        error("Card Grade", "Choose a card grade.")
    elif company is not None and grade.company_id != company.pk:
        error("Card Grade", "Choose a card grade of this company.")
    elif str(data.get("card_type") or "") != str(grade.card_type_id):
        error("Card Grade", "This grade is not of the chosen card type.")

    valid_from, valid_to = _date(data.get("valid_from")), _date(data.get("valid_to"))
    if valid_from is None:
        error("From Date", "Choose a date.")
    if valid_to is None:
        error("To Date", "Choose a date.")
    if valid_from and valid_to and valid_to < valid_from:
        error("To Date", "Must be on or after From Date.")

    days = {}
    if data.get("all_days"):
        percent = _percent(data.get("all_days_percent"))
        if percent is None:
            error("All Days", "Enter a discount between 0.01 and 100.")
        else:
            days = {day: percent for day in WeekDay.values}
    else:
        for row in data.get("days") or []:
            day = row.get("weekday") if isinstance(row, dict) else None
            if day not in WeekDay.values or day in days:
                continue
            percent = _percent(row.get("percent"))
            if percent is None:
                error(WeekDay(day).label, "Enter a discount between 0.01 and 100.")
            else:
                days[day] = percent
        if not days and not any(e["field"] in WeekDay.labels for e in errors):
            error("Days", "Choose all days or at least one day.")

    promotion = _PROMOTION.get(data.get("promotion"))
    if promotion is None:
        error("Promotion Type", "Choose a promotion type.")

    usage_type = data.get("usage_type")
    usage_limit = None
    if usage_type not in UsageType.values:
        error("Usage Type", "Choose a usage type.")
    elif usage_type != UsageType.ONE_TIME:
        try:
            usage_limit = int(data.get("usage_limit"))
        except (TypeError, ValueError):
            usage_limit = None
        if usage_limit is None or not 1 <= usage_limit <= MAX_LIMIT:
            error("Usage Type", "Enter how many times, 1 or more.")

    return {
        "company": company, "card_grade": grade, "valid_from": valid_from, "valid_to": valid_to,
        "requires_approval": promotion[0] if promotion else False,
        "fare_basis": promotion[1] if promotion else "",
        "usage_type": usage_type, "usage_limit": usage_limit,
        "is_active": bool(data.get("is_active", True)),
    }, days


def save_card_discount(user, data):
    """Create or update one discount with its days. Raises Invalid (every
    problem at once) or NotFound."""
    pk = data.get("pk") or None
    discount = None
    if pk:
        discount = card_discounts_for(user).filter(pk=pk).first()
        if discount is None:
            raise NotFound()

    errors = []
    values, days = _parse(user, data, errors)
    if errors:
        raise Invalid(errors)

    if values["is_active"]:
        overlapping = CardDiscount.objects.filter(
            card_grade=values["card_grade"], is_active=True,
            valid_from__lte=values["valid_to"], valid_to__gte=values["valid_from"],
        )
        if discount is not None:
            overlapping = overlapping.exclude(pk=discount.pk)
        if overlapping.exists():
            raise Invalid([{"field": "From Date", "message": OVERLAP}])

    discount = discount or CardDiscount(created_by=user)
    for field, value in values.items():
        setattr(discount, field, value)
    discount.modified_by = user
    try:
        with transaction.atomic():
            discount.save()
            discount.days.all().delete()
            CardDiscountDay.objects.bulk_create(
                CardDiscountDay(card_discount=discount, weekday=day, discount_percent=percent)
                for day, percent in sorted(days.items())
            )
    except IntegrityError:
        # Another save covering the same dates landed first.
        raise Invalid([{"field": "From Date", "message": OVERLAP}]) from None
    return discount


# -- Claims ------------------------------------------------------------------------


class NotPending(Exception):
    pass


def decide_claim(user, claim, approve, remarks=""):
    """Approve or reject a pending claim. Anything else is already decided."""
    with transaction.atomic():
        claim = type(claim).objects.select_for_update().get(pk=claim.pk)
        if claim.status != ClaimStatus.PENDING:
            raise NotPending()
        claim.status = ClaimStatus.APPROVED if approve else ClaimStatus.REJECTED
        claim.decided_at = timezone.now()
        claim.decided_by = user
        claim.remarks = (remarks or "").strip()[:255]
        claim.modified_by = user
        claim.save(update_fields=["status", "decided_at", "decided_by", "remarks", "modified_by", "modified_on"])
    return claim


# -- Customer card usage (apps/discount/api.py::CardUsageView) ---------------------


class DiscountRefused(Exception):
    """code/message/status for the envelope."""

    def __init__(self, code, message, status):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


def _customer_by_full_number(company, full_number):
    """This company's customer with that full number, not blocked -- or the
    refusal the device reads."""
    customer = Customer.objects.filter(company=company, mobile_full=full_number).first()
    if customer is None:
        raise DiscountRefused("unknown_customer", "No customer found.", 404)
    if customer.is_blocked:
        raise DiscountRefused("customer_blocked", "The customer is blocked.", 403)
    return customer


def card_usage(company, full_number):
    """(customer, rows) -- one row per discount that can be used today, with the
    customer's redemptions of it as they are: the device applies the usage
    window (day, last 7 days, month, period), so nothing is counted here beyond
    a total.

    Only redeemed claims count -- approved where approval is needed, and applied
    on a closed bill. Redemptions before a discount's From Date are its
    predecessor's business, not this one's.
    """
    customer = _customer_by_full_number(company, full_number)

    zone = zone_for(company)
    today = business_date_for(company)
    discounts = list(
        CardDiscount.objects
        .filter(company=company, is_active=True, valid_from__lte=today, valid_to__gte=today,
                card_grade__is_active=True, card_grade__card_type__is_active=True)
        .select_related("card_grade__card_type")
        .order_by("card_grade__card_type__name", "card_grade__name", "pk")
    )

    def day_start(day):
        return datetime.datetime.combine(day, datetime.time.min, tzinfo=zone)

    redeemed = {discount.pk: [] for discount in discounts}
    if discounts:
        claims = (
            CardDiscountClaim.objects
            .filter(customer=customer, status=ClaimStatus.REDEEMED, card_discount_id__in=list(redeemed),
                    redeemed_at__gte=day_start(min(d.valid_from for d in discounts)))
            .order_by("redeemed_at")
            .values_list("card_discount_id", "redeemed_at")
        )
        for discount_id, redeemed_at in claims:
            redeemed[discount_id].append(redeemed_at)

    rows = []
    for discount in discounts:
        start = day_start(discount.valid_from)
        times = [moment.astimezone(zone) for moment in redeemed[discount.pk] if moment >= start]
        rows.append((discount, times))
    return customer, rows


# -- Approval request (apps/discount/api.py::ApprovalRequestView) ------------------


def request_approval(session, values):
    """A pending claim for an approval-mode card discount on one order.
    Returns (claim, created): created is False when this sync_id was already
    stored, and the claim carries its current status.

    The bill figures are the device's, stored as sent. Beyond the lookups:
    one pending request per order at a time, and none while the order has a
    manager discount request waiting or approved (apps/rental/requests.py) --
    the order row is locked so the two never pass each other.
    """
    company = session.branch.company
    sync_id = values["sync_id"]
    existing = CardDiscountClaim.objects.filter(pk=sync_id).first()
    if existing is not None:
        if existing.company_id != company.pk:
            raise DiscountRefused("sync_id_conflict", "That sync_id is already used.", 409)
        return existing, False

    with transaction.atomic():
        return _request_approval(session, values, company, sync_id)


def _request_approval(session, values, company, sync_id):
    order = Order.objects.select_for_update().filter(pk=values["order_id"], company=company).first()
    if order is None:
        raise DiscountRefused("unknown_order", "No order with that id.", 404)
    if OrderRequest.objects.filter(order=order, kind=OrderRequestKind.DISCOUNT, status__in=REQUEST_LIVE).exists():
        raise DiscountRefused("discount_requested", "This order already has a manager discount request.", 409)
    customer = _customer_by_full_number(company, values["full_number"])
    discount = (CardDiscount.objects.select_related("card_grade__card_type")
                .filter(pk=values["card_discount_id"], company=company).first())
    if discount is None:
        raise DiscountRefused("unknown_card_discount", "No card discount with that id.", 404)
    if not discount.requires_approval:
        raise DiscountRefused("approval_not_needed", "This card discount needs no approval.", 400)
    if CardDiscountClaim.objects.filter(order=order, status=ClaimStatus.PENDING).exists():
        raise DiscountRefused("request_pending", "This order already has a pending approval request.", 409)

    claim = CardDiscountClaim(
        id=sync_id, company=company, card_discount=discount,
        card_type=discount.card_grade.card_type, card_grade=discount.card_grade,
        fare_basis=discount.fare_basis, requires_approval=True,
        customer=customer, customer_name=customer.full_name, mobile_full=customer.mobile_full,
        card_number=values.get("card_number") or "", card_photo=values.get("card_photo") or "",
        order=order, bill_amount=values["bill_amount"], discount_percent=values["discount_percent"],
        discount_amount=values["discount_amount"], net_amount=values["net_amount"],
        branch=session.branch, requested_by=session.user,
        rms_installation_id=session.device.installation_id if session.device_id else "",
        status=ClaimStatus.PENDING, requested_at=values["requested_at"],
        created_by=session.user, modified_by=session.user,
    )
    try:
        with transaction.atomic():
            claim.save(force_insert=True)
    except IntegrityError:
        # Lost a race: the same sync_id, or another pending request for this order.
        raced = CardDiscountClaim.objects.filter(pk=sync_id).first()
        if raced is not None:
            return raced, False
        raise DiscountRefused("request_pending", "This order already has a pending approval request.", 409) \
            from None
    return claim, True


def cancel_pending_for_order(order, now=None):
    """The order was billed while its approval request was still pending: the
    request is cancelled. For the order-close / billing API (not built yet)."""
    return CardDiscountClaim.objects.filter(order=order, status=ClaimStatus.PENDING).update(
        status=ClaimStatus.CANCELLED, decided_at=now or timezone.now(), modified_on=timezone.now(),
    )


# -- At settlement (apps/rental/services.py::settle_order) ---------------------------


class RedeemRefused(Exception):
    """The discount named at settle cannot be applied. `code` is one of the
    order API's refusals (unknown_card_claim, card_claim_not_approved,
    unknown_card_discount)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def redeem_for_order(session, order, discount, *, at, bill_amount, net_amount):
    """The card discount the tablet applied to `order`'s bill, redeemed.
    Returns the claim.

    An approval-mode discount names its approved claim (`request_sync_id`), which
    becomes redeemed. An automatic one names the discount
    (`card_discount_id`): there was no request, so a new claim is written,
    redeemed at once -- the claim table is the redemption history the usage
    limits count. Either way the claim's figures become the ones applied.
    Every other claim on the order still pending or approved is cancelled
    (order_lifecycle_design.md 5, decision 5).
    """
    figures = {
        "bill_amount": bill_amount, "discount_percent": discount["discount_percentage"],
        "discount_amount": discount["discount_amount"], "net_amount": net_amount,
    }
    if discount.get("request_sync_id"):
        claim = (CardDiscountClaim.objects.select_for_update()
                 .filter(pk=discount["request_sync_id"], order=order).first())
        if claim is None:
            raise RedeemRefused("unknown_card_claim")
        if claim.status != ClaimStatus.APPROVED:
            raise RedeemRefused("card_claim_not_approved")
        for field, value in figures.items():
            setattr(claim, field, value)
        claim.status, claim.redeemed_at, claim.modified_by = ClaimStatus.REDEEMED, at, session.user
        claim.save()
    else:
        card = (CardDiscount.objects.select_related("card_grade__card_type")
                .filter(pk=discount["card_discount_id"], company=order.company).first())
        if card is None:
            raise RedeemRefused("unknown_card_discount")
        claim = CardDiscountClaim.objects.create(
            id=uuid7(), company=order.company, card_discount=card, card_type=card.card_grade.card_type,
            card_grade=card.card_grade, fare_basis=card.fare_basis, requires_approval=False,
            customer=order.customer, customer_name=order.customer_name or order.customer.full_name,
            mobile_full=order.customer_mobile or order.customer.mobile_full,
            card_number=discount.get("card_number") or "", order=order, branch=session.branch,
            requested_by=session.user,
            rms_installation_id=session.device.installation_id if session.device_id else "",
            status=ClaimStatus.REDEEMED, requested_at=at, redeemed_at=at,
            created_by=session.user, modified_by=session.user, **figures,
        )
    cancel_unused_for_order(order, keep=claim, now=at)
    return claim


def cancel_unused_for_order(order, *, keep=None, now=None):
    """The order was billed: every claim on it still pending, or approved but
    not the one applied (`keep`), is cancelled. Returns how many."""
    now = now or timezone.now()
    unused = CardDiscountClaim.objects.filter(order=order, status__in=[ClaimStatus.PENDING, ClaimStatus.APPROVED])
    if keep is not None:
        unused = unused.exclude(pk=keep.pk)
    return unused.update(
        status=ClaimStatus.CANCELLED, decided_at=Coalesce(F("decided_at"), Value(now)), modified_on=timezone.now(),
    )


def approval_status(company, order_id, sync_id):
    """The one approval request with this sync_id, on this order, of this
    company -- or request_not_found, whichever of the three does not match."""
    claim = (CardDiscountClaim.objects.select_related("card_type", "card_grade")
             .filter(pk=sync_id, order_id=order_id, company=company).first())
    if claim is None:
        raise DiscountRefused("request_not_found", "No approval request found.", 404)
    return claim
