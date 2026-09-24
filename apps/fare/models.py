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
exclusion constraints on overlapping rules, seasons and live fares. They are
DEFERRABLE INITIALLY IMMEDIATE -- checked at once for any caller, deferred by
services.save_fare alone while it rewrites a fare's rows in one transaction.
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import ArrayField, RangeBoundary, RangeOperators
from django.contrib.postgres.indexes import OpClass
from django.db import models
from django.db.models import CheckConstraint, Deferrable, F, Q, UniqueConstraint

from apps.company.models import WeekDay
from apps.fare.db import DateRangeFunc, IntRangeFunc
from core.models import ApprovalMixin, TimeStampedModel

MIDNIGHT = 1440
EQUAL, OVERLAPS = RangeOperators.EQUAL, RangeOperators.OVERLAPS
IMMEDIATE = Deferrable.IMMEDIATE


def dates(start, end):
    """Both ends included: a fare valid to 31 Dec is valid on 31 Dec."""
    return DateRangeFunc(start, end, RangeBoundary(inclusive_lower=True, inclusive_upper=True))


MINUTES = IntRangeFunc("start_minute", "end_minute")   # [start, end): 10-11 and 11-12 meet, never overlap


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
    prices. On its dates the fare's own base and rules are not used."""

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
            ExclusionConstraint(
                name="fare_season_no_overlap",
                expressions=[("fare", EQUAL), (dates("start_date", "end_date"), OVERLAPS)],
                deferrable=IMMEDIATE,
                violation_error_message="A date can belong to one season only.",
            ),
        ]

    def __str__(self):
        return self.name


def _rule_overlap(name, kind, list_field, extra=(), **condition):
    return ExclusionConstraint(
        name=name,
        expressions=[(list_field, EQUAL), *extra, (MINUTES, OVERLAPS)],
        condition=Q(kind=kind, **condition),
        deferrable=IMMEDIATE,
        violation_error_message="Two prices of the same kind cannot cover the same time.",
    )


class FareRule(PricedModel):
    """A special price: every day, on selected days, or on a single date,
    inside a time window. season is empty for the fare's own rules."""

    fare = models.ForeignKey(Fare, on_delete=models.CASCADE, related_name="rules")
    season = models.ForeignKey(FareSeason, null=True, blank=True, on_delete=models.CASCADE, related_name="rules")
    kind = models.CharField(max_length=16, choices=RuleKind.choices)
    # int4[], not smallint[]: intarray's GiST operator class (overlap on days
    # in the exclusion constraint) only takes int4[].
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
            # One constraint per price list: the fare's own, and each season's.
            _rule_overlap("fare_rule_every_day_fare", RuleKind.EVERY_DAY, "fare", season__isnull=True),
            _rule_overlap("fare_rule_every_day_season", RuleKind.EVERY_DAY, "season", season__isnull=False),
            _rule_overlap("fare_rule_days_fare", RuleKind.SELECTED_DAYS, "fare",
                          extra=[(OpClass("weekdays", name="gist__int_ops"), OVERLAPS)], season__isnull=True),
            _rule_overlap("fare_rule_days_season", RuleKind.SELECTED_DAYS, "season",
                          extra=[(OpClass("weekdays", name="gist__int_ops"), OVERLAPS)], season__isnull=False),
            _rule_overlap("fare_rule_single_date", RuleKind.SINGLE_DATE, "fare", extra=[("on_date", EQUAL)]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.start_minute}-{self.end_minute}"
