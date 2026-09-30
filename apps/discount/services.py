"""Discount Card rules. Views only translate HTTP.

save_card_discount() -- the Card Discount page's one post: the discount and
its day rows, checked together and written in one transaction.
decide_claim() -- an approver's Approve / Reject on a pending claim.
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.company.models import WeekDay
from apps.company.scoping import companies_for
from apps.discount.models import (
    CardDiscount,
    CardDiscountDay,
    ClaimStatus,
    FareBasis,
    UsageType,
)
from apps.discount.scoping import card_discounts_for, card_grades_for

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
