"""Discount Card.

A Card Type (e.g. Corporate) has Grades (Gold, Silver); a Card Discount says
what a grade gets: a % per weekday, on the base or the full fare, applied
automatically or after approval, within a validity period and a usage limit.
A Card Discount Claim is one use of a card on a bill.

Ported from the legacy RMS tables: RMSCardType, RMSCardGrade, RMSCardDiscount
(saved by RMS_SaveCardDiscount, read by GetCardDiscount), RMSCardPromotionType
(4 rows) and RMSCardUsageType (5 rows); the claim replaces DMSOrderCardDiscount.
The mapping and what was dropped are in design/discount/discount-card.md.

No approval workflow on the three masters (decided with the client) -- only
Active. There is no card master: the device user picks type and grade and may
type the card number, which is kept on the claim.
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeBoundary, RangeOperators
from django.db import models
from django.db.models import CheckConstraint, F, Q, UniqueConstraint

from apps.company.models import WeekDay
from apps.fare.db import DateRangeFunc
from core.models import TimeStampedModel


class CardType(TimeStampedModel):
    """Legacy RMSCardType."""

    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="card_types")
    code = models.CharField("Card Type Code", max_length=20)
    name = models.CharField("Card Type Name", max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "card_type"
        verbose_name_plural = "card types"
        ordering = ["name"]
        constraints = [
            UniqueConstraint(fields=["company", "code"], name="uniq_card_type_code",
                             violation_error_message="A card type with this code already exists."),
            UniqueConstraint(fields=["company", "name"], name="uniq_card_type_name",
                             violation_error_message="A card type with this name already exists."),
        ]
        indexes = [models.Index(fields=["company"])]

    def __str__(self):
        return self.name


class CardGrade(TimeStampedModel):
    """Legacy RMSCardGrade. Codes and names are unique within their card
    type -- two types may both have a "Gold". The grade's company is always
    its type's (forms.CardGradeForm)."""

    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="card_grades")
    card_type = models.ForeignKey(CardType, on_delete=models.PROTECT, related_name="grades")
    code = models.CharField("Grade Code", max_length=20)
    name = models.CharField("Grade Name", max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "card_grade"
        verbose_name_plural = "card grades"
        ordering = ["card_type__name", "name"]
        constraints = [
            UniqueConstraint(fields=["card_type", "code"], name="uniq_card_grade_code",
                             violation_error_message="This card type already has a grade with this code."),
            UniqueConstraint(fields=["card_type", "name"], name="uniq_card_grade_name",
                             violation_error_message="This card type already has a grade with this name."),
        ]
        indexes = [models.Index(fields=["company"]), models.Index(fields=["card_type"])]

    def __str__(self):
        return f"{self.card_type} · {self.name}"


class FareBasis(models.TextChoices):
    BASE = "base", "Base Fare"
    FULL = "full", "Full Fare"


class UsageType(models.TextChoices):
    """Legacy RMSCardUsageType, all 5 rows. The limit counts per customer;
    the device applies the window (calendar day, last 7 days, calendar month,
    the discount's own validity)."""

    ONE_TIME = "one_time", "One time"
    PER_DAY = "per_day", "Times a day"
    PER_WEEK = "per_week", "Times a week"
    PER_MONTH = "per_month", "Times a month"
    PER_PERIOD = "per_period", "Times in the period"


def _validity():
    return DateRangeFunc("valid_from", "valid_to", RangeBoundary(inclusive_lower=True, inclusive_upper=True))


class CardDiscount(TimeStampedModel):
    """Legacy RMSCardDiscount. The four legacy promotion types
    (RMSCardPromotionType) are two choices here: requires_approval x
    fare_basis. The seven legacy day columns are CardDiscountDay rows.

    One active discount per grade on any date: RMS_SaveCardDiscount switched
    an overlapping one off silently; here an overlap is refused instead
    (client decision), by the exclusion constraint below."""

    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="card_discounts")
    card_grade = models.ForeignKey(CardGrade, on_delete=models.PROTECT, related_name="discounts")
    valid_from = models.DateField("From Date")
    valid_to = models.DateField("To Date")
    requires_approval = models.BooleanField(default=False)
    fare_basis = models.CharField(max_length=4, choices=FareBasis.choices)
    usage_type = models.CharField(max_length=10, choices=UsageType.choices)
    usage_limit = models.PositiveSmallIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "card_discount"
        verbose_name_plural = "card discounts"
        ordering = ["-valid_from"]
        constraints = [
            CheckConstraint(condition=Q(valid_to__gte=F("valid_from")), name="card_discount_dates"),
            CheckConstraint(
                condition=Q(usage_type=UsageType.ONE_TIME, usage_limit__isnull=True)
                # isnull=False spelled out: a NULL limit would otherwise pass,
                # since NULL >= 1 is unknown and a CHECK lets unknown through.
                | (~Q(usage_type=UsageType.ONE_TIME) & Q(usage_limit__isnull=False, usage_limit__gte=1)),
                name="card_discount_usage_limit",
            ),
            ExclusionConstraint(
                name="card_discount_no_overlap",
                expressions=[("card_grade", RangeOperators.EQUAL), (_validity(), RangeOperators.OVERLAPS)],
                condition=Q(is_active=True),
                violation_error_message="Another active discount covers these dates for this grade.",
            ),
        ]
        indexes = [models.Index(fields=["company"]), models.Index(fields=["card_grade"])]

    def __str__(self):
        return f"{self.card_grade} · {self.valid_from:%-d %b %Y} – {self.valid_to:%-d %b %Y}"

    @property
    def promotion_label(self):
        mode = "Approval" if self.requires_approval else "Automatic"
        return f"{mode} · {self.get_fare_basis_display()}"

    @property
    def usage_label(self):
        if self.usage_type == UsageType.ONE_TIME:
            return "One time"
        return f"{self.usage_limit} {self.get_usage_type_display().lower()}"


class CardDiscountDay(models.Model):
    """A weekday the discount applies on, and its %. A day with no row gets
    no discount; "all days" is seven rows with the same %."""

    card_discount = models.ForeignKey(CardDiscount, on_delete=models.CASCADE, related_name="days")
    weekday = models.PositiveSmallIntegerField(choices=WeekDay.choices)
    discount_percent = models.DecimalField("Discount %", max_digits=5, decimal_places=2)

    class Meta:
        db_table = "card_discount_day"
        ordering = ["weekday"]
        constraints = [
            UniqueConstraint(fields=["card_discount", "weekday"], name="uniq_card_discount_day"),
            CheckConstraint(condition=Q(discount_percent__gt=0, discount_percent__lte=100),
                            name="card_discount_day_percent"),
            CheckConstraint(condition=Q(weekday__lte=6), name="card_discount_day_weekday"),
        ]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.discount_percent}%"


class ClaimStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    # Still pending when its order was billed -- set by the order-close API
    # (services.cancel_pending_for_order), never by a person.
    CANCELLED = "cancelled", "Cancelled"
    REDEEMED = "redeemed", "Redeemed"


class CardDiscountClaim(TimeStampedModel):
    """One use of a card on a bill -- replaces legacy DMSOrderCardDiscount.

    An approval-mode discount starts pending, is approved or rejected on the
    web, and becomes redeemed when the bill closes; an automatic one is
    redeemed at once. Only redeemed claims count against a usage limit.

    Written by the device APIs (apps/discount/api.py). The discount's type,
    grade and fare basis are copied in as they were when claimed, so later
    edits to the discount never rewrite history. The bill figures -- bill
    amount, %, discount amount, net -- are the device's own, stored as sent
    and only displayed. card_photo is the link the device sends after
    uploading the photo to the photo store. At most one request per order is
    pending at a time (uniq_pending_claim_per_order).
    """

    id = models.UUIDField(primary_key=True, editable=False)
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="card_claims")
    card_discount = models.ForeignKey(CardDiscount, on_delete=models.PROTECT, related_name="claims")
    card_type = models.ForeignKey(CardType, on_delete=models.PROTECT, related_name="claims")
    card_grade = models.ForeignKey(CardGrade, on_delete=models.PROTECT, related_name="claims")
    discount_percent = models.DecimalField("Discount %", max_digits=5, decimal_places=2)
    fare_basis = models.CharField(max_length=4, choices=FareBasis.choices)
    requires_approval = models.BooleanField()

    customer = models.ForeignKey("rental.Customer", on_delete=models.PROTECT, related_name="card_claims")
    customer_name = models.CharField(max_length=200)
    mobile_full = models.CharField("Phone", max_length=30)
    card_number = models.CharField(max_length=50, blank=True)
    card_photo = models.CharField(max_length=500, blank=True)

    order = models.ForeignKey("rental.Order", on_delete=models.PROTECT, related_name="card_claims")
    bill_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    branch = models.ForeignKey("company.Branch", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="card_claims")
    requested_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    rms_installation_id = models.CharField(max_length=64, blank=True)

    status = models.CharField(max_length=10, choices=ClaimStatus.choices)
    requested_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    remarks = models.CharField(max_length=255, blank=True)
    redeemed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "card_discount_claim"
        ordering = ["-requested_at"]
        constraints = [
            CheckConstraint(condition=Q(discount_percent__gt=0, discount_percent__lte=100),
                            name="card_claim_percent"),
            CheckConstraint(
                condition=(
                    Q(status=ClaimStatus.PENDING, decided_at__isnull=True, decided_by__isnull=True,
                      redeemed_at__isnull=True, requires_approval=True)
                    | Q(status__in=[ClaimStatus.APPROVED, ClaimStatus.REJECTED], decided_at__isnull=False,
                        redeemed_at__isnull=True, requires_approval=True)
                    | Q(status=ClaimStatus.CANCELLED, decided_at__isnull=False, redeemed_at__isnull=True,
                        requires_approval=True)
                    | Q(status=ClaimStatus.REDEEMED, redeemed_at__isnull=False, requires_approval=False)
                    | Q(status=ClaimStatus.REDEEMED, redeemed_at__isnull=False, requires_approval=True,
                        decided_at__isnull=False)
                ),
                name="card_claim_status_fields",
            ),
            UniqueConstraint(
                fields=["order"], condition=Q(status=ClaimStatus.PENDING), name="uniq_pending_claim_per_order",
                violation_error_message="This order already has a pending approval request.",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "requested_at"]),
            models.Index(fields=["customer", "card_discount", "redeemed_at"]),
            models.Index(fields=["company", "requested_at"]),
        ]

    def __str__(self):
        return f"{self.customer_name} · {self.card_grade} · {self.get_status_display()}"
