"""Fares -- what a vehicle type costs, where, and when.

New, not ported: the legacy RmsFareDetails (two rows per vehicle type, no
branch scope) is not migrated; design/fares and offers/fare-schema.md section 7
maps its columns. The design, the precedence and why each rule exists are in
that document; apps.fare.pricing is the code that applies them.

One fare prices one vehicle type for one package time, at company level or for
chosen branches, over a date range. Its own base price and special prices
(`FareRule`) apply unless a season (`FareSeason`) covers the date -- a season
brings its own base price and special prices. Single dates live on the fare
and win everywhere, seasons included.

The database refuses anything that could give two prices for one minute:
exclusion constraints on live fares that overlap, and constraint triggers
(migration 0002) on rules and seasons that cross or are identical -- nested
ones are allowed, the innermost wins (apps.fare.pricing). All are DEFERRABLE
INITIALLY IMMEDIATE -- checked at once for any caller, deferred by
services.save_fare alone while it rewrites a fare's rows in one transaction.
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import ArrayField, RangeBoundary, RangeOperators
from django.db import models
from django.db.models import CheckConstraint, Deferrable, F, Q, UniqueConstraint

from apps.company.models import WeekDay
from apps.fare.db import DateRangeFunc
from core.models import ApprovalMixin, TimeStampedModel

MIDNIGHT = 1440
MAX_MINUTES = 1440     # package time, intervals and graces: at most one day
EQUAL, OVERLAPS = RangeOperators.EQUAL, RangeOperators.OVERLAPS
IMMEDIATE = Deferrable.IMMEDIATE


def dates(start, end):
    """Both ends included: a fare valid to 31 Dec is valid on 31 Dec."""
    return DateRangeFunc(start, end, RangeBoundary(inclusive_lower=True, inclusive_upper=True))


class FareLevel(models.TextChoices):
    COMPANY = "company", "Company"
    BRANCH = "branch", "Branch"


class RuleKind(models.TextChoices):
    EVERY_DAY = "every_day", "Every day"
    SELECTED_DAYS = "selected_days", "Selected days"
    SINGLE_DATE = "single_date", "Single date"


class PricedModel(models.Model):
    """The five numbers that price a rental -- the same on a fare, a season and
    a rule. Constraints are added per model (price_checks), since a child's own
    Meta.constraints would replace any declared here."""

    base_fare = models.DecimalField("Basic Fare", max_digits=12, decimal_places=2)
    grace_minutes = models.PositiveSmallIntegerField("Grace Period", default=0)
    concurrent_interval_minutes = models.PositiveSmallIntegerField("Concurrent Interval")
    concurrent_fare = models.DecimalField("Concurrent Fare", max_digits=12, decimal_places=2)
    concurrent_grace_minutes = models.PositiveSmallIntegerField("Concurrent Grace", default=0)

    class Meta:
        abstract = True


def price_checks(prefix):
    return [
        CheckConstraint(condition=Q(base_fare__gte=0, concurrent_fare__gte=0), name=f"{prefix}_money_not_negative"),
        CheckConstraint(condition=Q(concurrent_interval_minutes__gt=0), name=f"{prefix}_interval_positive"),
        CheckConstraint(
            condition=Q(grace_minutes__lte=MAX_MINUTES, concurrent_interval_minutes__lte=MAX_MINUTES,
                        concurrent_grace_minutes__lte=MAX_MINUTES),
            name=f"{prefix}_minutes_max",
        ),
    ]


class Fare(ApprovalMixin, TimeStampedModel, PricedModel):
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="fares")
    vehicle_type = models.ForeignKey("fleet.VehicleType", on_delete=models.PROTECT, related_name="fares")
    level = models.CharField("Fare Level", max_length=10, choices=FareLevel.choices)
    valid_from = models.DateField("Valid From")
    valid_to = models.DateField("Valid To")
    package_minutes = models.PositiveSmallIntegerField("Package Time")
    branches = models.ManyToManyField("company.Branch", through="FareBranch", related_name="fares", blank=True)

    # Optimistic locking: the form carries the value it loaded, and a save on
    # a copy someone else has saved since is refused rather than overwriting
    # their seasons and prices (services.save_fare).
    lock_version = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "fare"
        ordering = ["-valid_from", "vehicle_type__vehicle_type_name"]
        constraints = [
            CheckConstraint(condition=Q(valid_to__gte=F("valid_from")), name="fare_valid_dates"),
            CheckConstraint(condition=Q(package_minutes__gt=0), name="fare_package_positive"),
            CheckConstraint(condition=Q(package_minutes__lte=MAX_MINUTES), name="fare_package_max"),
            *price_checks("fare"),
            # Target of fare_branch's composite foreign key (migration 0001):
            # the branch links' copies can only ever equal their fare's.
            UniqueConstraint(
                fields=["id", "level", "vehicle_type", "package_minutes", "valid_from", "valid_to", "is_active"],
                name="fare_copy_key",
            ),
            ExclusionConstraint(
                name="fare_company_no_overlap",
                expressions=[
                    ("company", EQUAL), ("vehicle_type", EQUAL), ("package_minutes", EQUAL),
                    (dates("valid_from", "valid_to"), OVERLAPS),
                ],
                condition=Q(level=FareLevel.COMPANY, is_active=True),
                deferrable=IMMEDIATE,
                violation_error_message="Another active company fare already covers these dates.",
            ),
        ]

    def __str__(self):
        return (f"{self.vehicle_type} · {self.package_minutes} min · "
                f"{self.valid_from:%-d %b %Y} – {self.valid_to:%-d %b %Y}")


class FareBranch(models.Model):
    """A branch a branch-level fare covers. Company-level fares have none.

    level, vehicle_type, package_minutes, dates and is_active are copies of
    the fare's, held so the exclusion constraint below can see them -- a
    constraint cannot reach across to `fare`. A composite foreign key with ON
    UPDATE CASCADE (migration 0001) keeps them equal to the fare's: Postgres,
    not the app, pushes every change down. Rows are created and deleted, never
    re-saved.
    """

    fare = models.ForeignKey(Fare, on_delete=models.CASCADE, related_name="branch_links")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="fare_links")
    level = models.CharField(max_length=10, choices=FareLevel.choices, default=FareLevel.BRANCH)
    vehicle_type = models.ForeignKey(
        "fleet.VehicleType", on_delete=models.DO_NOTHING, db_constraint=False, related_name="+",
    )
    package_minutes = models.PositiveSmallIntegerField()
    valid_from = models.DateField()
    valid_to = models.DateField()
    is_active = models.BooleanField()

    class Meta:
        db_table = "fare_branch"
        verbose_name = "branch fare"
        verbose_name_plural = "branch fares"
        constraints = [
            UniqueConstraint(fields=["fare", "branch"], name="fare_branch_once"),
            CheckConstraint(condition=Q(level=FareLevel.BRANCH), name="fare_branch_branch_level_only"),
            ExclusionConstraint(
                name="fare_branch_no_overlap",
                expressions=[
                    ("branch", EQUAL), ("vehicle_type", EQUAL), ("package_minutes", EQUAL),
                    (dates("valid_from", "valid_to"), OVERLAPS),
                ],
                condition=Q(is_active=True),
                deferrable=IMMEDIATE,
                violation_error_message="Another active fare already covers this branch on these dates.",
            ),
        ]

    def __str__(self):
        return f"{self.fare} · {self.branch}"

    def save(self, *args, **kwargs):
        fare = self.fare
        self.level = fare.level
        self.vehicle_type_id = fare.vehicle_type_id
        self.package_minutes = fare.package_minutes
        self.valid_from, self.valid_to = fare.valid_from, fare.valid_to
        self.is_active = fare.is_active
        super().save(*args, **kwargs)


class FareSeason(PricedModel):
    """A date range inside the fare with its own base price and special
    prices. On its dates the fare's own base and rules are not used. Seasons
    may nest (Eid inside Summer): the innermost covering a date is used.
    Crossing or identical ranges are refused by the trigger
    fare_season_no_crossing (migration 0002)."""

    fare = models.ForeignKey(Fare, on_delete=models.CASCADE, related_name="seasons")
    name = models.CharField("Season Name", max_length=100)
    start_date = models.DateField("From Date")
    end_date = models.DateField("To Date")

    class Meta:
        db_table = "fare_season"
        ordering = ["start_date"]
        constraints = [
            CheckConstraint(condition=Q(end_date__gte=F("start_date")), name="fare_season_dates"),
            *price_checks("fare_season"),
            # Target of fare_rule's (season, fare) foreign key: a rule's season
            # always belongs to the rule's own fare.
            UniqueConstraint(fields=["id", "fare"], name="fare_season_id_fare"),
        ]

    def __str__(self):
        return self.name


class FareRule(PricedModel):
    """A special price: every day, on selected days, or on a single date,
    inside a time window. season is empty for the fare's own rules.

    Two rules of one kind in one list that share a day may nest (12-14 inside
    08-20; the innermost wins) or be apart; crossing or identical windows are
    refused by the trigger fare_rule_no_crossing (migration 0002)."""

    fare = models.ForeignKey(Fare, on_delete=models.CASCADE, related_name="rules")
    season = models.ForeignKey(FareSeason, null=True, blank=True, on_delete=models.CASCADE, related_name="rules")
    kind = models.CharField(max_length=16, choices=RuleKind.choices)
    # int4[] rather than smallint[]: kept from migration 0001, whose GiST
    # constraints needed it; the trigger's && (a shared day) works on either.
    weekdays = ArrayField(models.IntegerField(choices=WeekDay.choices), default=list, blank=True)
    on_date = models.DateField(null=True, blank=True)
    # Minutes from midnight: Python's time cannot hold 24:00, and "until
    # midnight" is common. The window is [start, end); 1440 is midnight.
    start_minute = models.PositiveSmallIntegerField()
    end_minute = models.PositiveSmallIntegerField()

    class Meta:
        db_table = "fare_rule"
        ordering = ["kind", "on_date", "start_minute"]
        constraints = [
            *price_checks("fare_rule"),
            CheckConstraint(
                condition=Q(end_minute__gt=F("start_minute"), end_minute__lte=MIDNIGHT),
                name="fare_rule_window",
            ),
            CheckConstraint(condition=Q(weekdays__contained_by=list(WeekDay.values)), name="fare_rule_weekday_values"),
            CheckConstraint(
                condition=(
                    Q(kind=RuleKind.EVERY_DAY, weekdays__len=0, on_date__isnull=True)
                    | Q(kind=RuleKind.SELECTED_DAYS, weekdays__len__gt=0, on_date__isnull=True)
                    | Q(kind=RuleKind.SINGLE_DATE, weekdays__len=0, on_date__isnull=False, season__isnull=True)
                ),
                name="fare_rule_kind_fields",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.start_minute}-{self.end_minute}"


# --- Offer / Promotion --------------------------------------------------------
#
# Built from the client's "Promotion And Offers Creation" mockup
# (byky-main/byky Docs/Scheme_Creation.html) and
# design/fares and offers/business-logic.md Part 2. Models, UI and CRUD only
# for this pass -- pricing/precedence and the device API are a separate,
# later decision (see that doc's open questions 10-19, none of which block
# the data model or the screen).
#
# Unlike Fare, there is no overlap/nesting invariant to enforce here (nothing
# in the spec asks whether two offers may cover the same vehicle type/dates),
# so this stays a plain parent + three child-row tables: no exclusion
# constraints, no constraint triggers, no lock_version -- add any of those
# later if the client's answers call for them.


class OfferLevel(models.TextChoices):
    COMPANY = "company", "Company Wise"
    BRANCH = "branch", "Branch Wise"
    LOCATION = "location", "Location Wise"


class PromotionFor(models.TextChoices):
    QUANTITY = "quantity", "Quantity"
    AMOUNT = "amount", "Amount"


class InventoryType(models.TextChoices):
    # Only value in the client mockup; kept as a real choice field rather
    # than hardcoded, since the mockup itself presents it as a dropdown.
    VEHICLE_TYPE = "vehicle_type", "Vehicle Type"


class PromotionType(models.TextChoices):
    QUANTITY = "quantity", "Quantity"
    AMOUNT = "amount", "Amount"
    PERCENTAGE = "percentage", "Percentage"
    EACH = "each", "Each"
    OFFER_PRICE = "offer_price", "Offer Price"


class FreeOrOfferPrice(models.TextChoices):
    FREE = "free", "Free"
    OFFER_PRICE = "offer_price", "Offer Price"


class SlabDateMode(models.TextChoices):
    ALL_DATES = "all_dates", "All Dates"
    SPECIFIC_DATE = "specific_date", "Specific Date"


class SlabDay(models.TextChoices):
    ALL_DAYS = "all_days", "All Days"
    MONDAY = "monday", "Monday"
    TUESDAY = "tuesday", "Tuesday"
    WEDNESDAY = "wednesday", "Wednesday"
    THURSDAY = "thursday", "Thursday"
    FRIDAY = "friday", "Friday"
    SATURDAY = "saturday", "Saturday"
    SUNDAY = "sunday", "Sunday"


class Offer(ApprovalMixin, TimeStampedModel):
    """A promotion: for rentals within a scope and validity, whose quantity or
    amount falls inside a band, gives a discount on selected packages or
    gives free items. "Package" here is always a plain package_minutes
    number, matching Fare.package_minutes -- no separate Package model
    exists, and offers never re-price a package, only refer to one that
    already exists in Fare Entry."""

    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="offers")
    offer_code = models.CharField("Promotion Code", max_length=30)
    offer_name = models.CharField("Promotion Name", max_length=100)

    level = models.CharField("Promotion Level", max_length=10, choices=OfferLevel.choices, default=OfferLevel.COMPANY)
    branch = models.ForeignKey(
        "company.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="offers",
    )
    location = models.ForeignKey(
        "company.Location", on_delete=models.PROTECT, null=True, blank=True, related_name="offers",
    )

    valid_from = models.DateField("Valid From")
    valid_to = models.DateField("Valid To")
    promotion_for = models.CharField("Promotion For", max_length=10, choices=PromotionFor.choices)
    inventory_type = models.CharField(
        "Inventory Type", max_length=20, choices=InventoryType.choices, default=InventoryType.VEHICLE_TYPE,
    )
    lower_value = models.DecimalField("Lower Promotion Value", max_digits=12, decimal_places=2)
    upper_value = models.DecimalField("Upper Promotion Value", max_digits=12, decimal_places=2)

    promotion_type = models.CharField("Promotion Type", max_length=12, choices=PromotionType.choices)

    # Number of Promotion Items / Number of Free Items are deliberately not
    # fields here -- they are how many rows to render, and the rows
    # themselves (below) are the real data. Persisting a count alongside the
    # rows it describes is the "derive from the child table" rule this
    # project already applies elsewhere.
    time_slab_applicable = models.BooleanField("Time Slab Applicable", default=False)
    free_item_selectable = models.BooleanField("Free Item Selectable", default=False)
    # The free-text box the mockup shows beside Free Item Selectable when on.
    # Its purpose is undefined (business-logic.md Q15) -- stored as typed,
    # no behaviour attached to it.
    free_item_selectable_note = models.CharField(max_length=255, blank=True)
    # One scheme-wide mode, not per-row: the mockup states this "applies to
    # the Free Promotion Items value field", reused for the common time
    # slabs' value column too.
    free_or_offer_price = models.CharField(
        "Free / Offer Price", max_length=12, choices=FreeOrOfferPrice.choices, default=FreeOrOfferPrice.FREE,
    )

    class Meta:
        db_table = "offer"
        ordering = ["-valid_from", "offer_name"]
        constraints = [
            UniqueConstraint(fields=["company", "offer_code"], name="uniq_offer_code_per_company"),
            CheckConstraint(condition=Q(valid_to__gte=F("valid_from")), name="offer_valid_dates"),
            CheckConstraint(condition=Q(upper_value__gte=F("lower_value")), name="offer_value_band"),
            # Exactly the FK matching the chosen level -- the other stays null.
            CheckConstraint(
                condition=(
                    Q(level=OfferLevel.COMPANY, branch__isnull=True, location__isnull=True)
                    | Q(level=OfferLevel.BRANCH, branch__isnull=False, location__isnull=True)
                    | Q(level=OfferLevel.LOCATION, branch__isnull=True, location__isnull=False)
                ),
                name="offer_scope_matches_level",
            ),
        ]

    def __str__(self):
        return f"{self.offer_code} · {self.offer_name}"


class OfferItem(models.Model):
    """One Promotion Item: a vehicle type + package this offer applies to.
    Plain child row, same shape as FareRule/FareSeason -- no audit fields of
    its own (PricedModel doesn't carry them either); the parent Offer's own
    audit trail covers it, and every save rewrites every child row wholesale
    (offer_services.save_offer), so per-row history isn't meaningful.

    value is null when the offer's promotion_type is Quantity (the mockup
    hides the value column entirely for that type); otherwise it's a
    discount amount, a percentage, or an offer price depending on
    promotion_type -- label/meaning only, same number either way.
    """

    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="items")
    vehicle_type = models.ForeignKey("fleet.VehicleType", on_delete=models.PROTECT, related_name="offer_items")
    package_minutes = models.PositiveSmallIntegerField()
    value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "offer_item"
        constraints = [
            CheckConstraint(condition=Q(package_minutes__gt=0, package_minutes__lte=MAX_MINUTES),
                             name="offer_item_package_range"),
            CheckConstraint(condition=Q(value__isnull=True) | Q(value__gte=0), name="offer_item_value_not_negative"),
        ]

    def __str__(self):
        return f"{self.vehicle_type} · {self.package_minutes} min"


class OfferFreeItem(models.Model):
    """One Free Promotion Item: only relevant when the offer's promotion_type
    is Quantity. Independent of OfferItem -- its own vehicle type, package
    and count, a buy-this-get-that. value is a whole free quantity when
    Offer.free_or_offer_price is Free, or an offer price (AED) when it's
    Offer Price -- meaning follows the parent's single scheme-wide mode."""

    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="free_items")
    vehicle_type = models.ForeignKey("fleet.VehicleType", on_delete=models.PROTECT, related_name="offer_free_items")
    package_minutes = models.PositiveSmallIntegerField()
    value = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        db_table = "offer_free_item"
        constraints = [
            CheckConstraint(condition=Q(package_minutes__gt=0, package_minutes__lte=MAX_MINUTES),
                             name="offer_free_item_package_range"),
            CheckConstraint(condition=Q(value__gte=0), name="offer_free_item_value_not_negative"),
        ]

    def __str__(self):
        return f"{self.vehicle_type} · {self.package_minutes} min"


class OfferFreeItemTimeSlab(models.Model):
    """One row of the offer's common 'Free Item Time Slabs' -- shared across
    the whole offer, not per Promotion Item (the mockup is explicit: "time
    slab is COMMON FOR THE WHOLE SCHEME"). Only relevant when
    Offer.time_slab_applicable is set. Day is a single choice here, unlike
    FareRule.weekdays' multi-select array -- the mockup's Common Time Slab
    grid has one Day dropdown per row, not a multi-day picker."""

    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="time_slabs")
    date_mode = models.CharField(max_length=15, choices=SlabDateMode.choices, default=SlabDateMode.ALL_DATES)
    specific_date = models.DateField(null=True, blank=True)
    day = models.CharField(max_length=10, choices=SlabDay.choices, default=SlabDay.ALL_DAYS)
    from_time = models.TimeField()
    to_time = models.TimeField()
    vehicle_type = models.ForeignKey("fleet.VehicleType", on_delete=models.PROTECT, related_name="offer_time_slabs")
    package_minutes = models.PositiveSmallIntegerField()
    value = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        db_table = "offer_free_item_time_slab"
        constraints = [
            CheckConstraint(condition=Q(to_time__gt=F("from_time")), name="offer_slab_time_order"),
            CheckConstraint(
                condition=Q(date_mode=SlabDateMode.ALL_DATES, specific_date__isnull=True)
                | Q(date_mode=SlabDateMode.SPECIFIC_DATE, specific_date__isnull=False),
                name="offer_slab_date_matches_mode",
            ),
            CheckConstraint(condition=Q(package_minutes__gt=0, package_minutes__lte=MAX_MINUTES),
                             name="offer_slab_package_range"),
            CheckConstraint(condition=Q(value__gte=0), name="offer_slab_value_not_negative"),
        ]

    def __str__(self):
        return f"{self.vehicle_type} · {self.from_time}-{self.to_time}"
