"""Station targets (design/08-targets.md, design/11-target-profiles.md).

A Target Profile is a named way of setting targets -- a metric, a period type
(daily, monthly, custom range, annual) and a date range -- assigned to
stations, each with its own amount. Profiles are independent and may overlap
on one station (11 P2); only the same type + metric over overlapping dates is
refused (P3), by the database.

A target is one generated period of one profile for one station. Its days
carry the planned amount (split once) and the required amount (frozen at that
business day's start). What was achieved is never stored: it is always worked
out from the orders (apps/targets/services.py).
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.db import models
from django.db.models import F, Func, Q

from apps.rental.models import MONEY
from core.models import TimeStampedModel


class TargetKind(models.TextChoices):
    """Sales (AED, split into days) or customers (a whole number of unique
    customers -- design/08-targets.md section 9). A profile's metric."""

    SALES = "sales", "Sales"
    CUSTOMERS = "customers", "Customers"


class PeriodType(models.TextChoices):
    DAILY = "daily", "Daily"
    MONTHLY = "monthly", "Monthly"
    CUSTOM = "custom", "Custom range"
    ANNUAL = "annual", "Annual"


class TargetProfile(TimeStampedModel):
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="target_profiles")
    name = models.CharField(max_length=100)
    metric = models.CharField(max_length=10, choices=TargetKind.choices, default=TargetKind.SALES)
    period_type = models.CharField(max_length=10, choices=PeriodType.choices, default=PeriodType.MONTHLY)
    # Daily: the days; Monthly: the months they fall in; Custom: the one range;
    # Annual: the financial years they fall in (11 §4).
    starts_on = models.DateField()
    ends_on = models.DateField()
    # One switch (owner, 20 Nov 2026): only an active profile makes targets.
    # Off before it has made any = still being set up; off afterwards = it
    # makes no new periods and keeps its history (existing targets stay).
    is_active = models.BooleanField(default=False)
    remarks = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "targets_profile"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=Q(ends_on__gte=F("starts_on")), name="targets_profile_dates"),
            models.UniqueConstraint(fields=["company", "name"], name="targets_profile_name"),
        ]

    def __str__(self):
        return self.name

    @property
    def is_customers(self):
        return self.metric == TargetKind.CUSTOMERS


class TargetProfileBranch(models.Model):
    """A station on a profile, with its amount per period: per day (Daily),
    per month (Monthly), the whole range (Custom, Annual)."""

    profile = models.ForeignKey(TargetProfile, on_delete=models.CASCADE, related_name="assignments")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="target_assignments")
    amount = models.DecimalField(**MONEY)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "targets_profile_branch"
        ordering = ["branch__name"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "branch"], name="targets_profile_branch_once"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="targets_profile_branch_amount"),
        ]

    def __str__(self):
        return f"{self.profile} · {self.branch}"


class TargetProfileMonth(models.Model):
    """An annual profile's share of the year per month, in financial-year
    order (1 = the year's first month); the 12 weights total 100 (11 P5)."""

    profile = models.ForeignKey(TargetProfile, on_delete=models.CASCADE, related_name="months")
    month_index = models.PositiveSmallIntegerField()
    weight = models.DecimalField(max_digits=6, decimal_places=3)

    class Meta:
        db_table = "targets_profile_month"
        ordering = ["month_index"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "month_index"], name="targets_profile_month_once"),
            models.CheckConstraint(condition=Q(month_index__gte=1, month_index__lte=12),
                                   name="targets_profile_month_index"),
            models.CheckConstraint(condition=Q(weight__gte=0), name="targets_profile_month_weight"),
        ]

    def __str__(self):
        return f"{self.profile} month {self.month_index}: {self.weight}%"


class DateRange(Func):
    """daterange(start, end, '[]') -- an inclusive range for the overlap rule."""

    function = "DATERANGE"
    output_field = models.Field()

    def __init__(self, start, end):
        super().__init__(start, end, models.Value("[]"))


class Target(TimeStampedModel):
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="+")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="targets")
    profile = models.ForeignKey(TargetProfile, on_delete=models.PROTECT, related_name="targets")
    kind = models.CharField(max_length=10, choices=TargetKind.choices, default=TargetKind.SALES)
    # the profile's, copied so the database can refuse a duplicate (11 P3)
    period_type = models.CharField(max_length=10, choices=PeriodType.choices, default=PeriodType.MONTHLY)
    period_start = models.DateField()           # first business day of the period
    period_end = models.DateField()             # last business day, inclusive
    amount = models.DecimalField(**MONEY)
    amount_overridden = models.BooleanField(default=False)   # this period edited on its own
    remarks = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "targets_target"
        ordering = ["branch__name", "period_start"]
        constraints = [
            models.CheckConstraint(condition=Q(period_end__gte=F("period_start")), name="targets_target_period"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="targets_target_amount"),
            models.UniqueConstraint(fields=["profile", "branch", "period_start"],
                                    name="targets_target_one_per_profile_period"),
            ExclusionConstraint(
                name="targets_target_no_duplicate",
                expressions=[("branch", RangeOperators.EQUAL), ("kind", RangeOperators.EQUAL),
                             ("period_type", RangeOperators.EQUAL),
                             (DateRange("period_start", "period_end"), RangeOperators.OVERLAPS)],
            ),
        ]
        indexes = [models.Index(fields=["company", "period_start"], name="targets_target_company_period"),
                   models.Index(fields=["branch", "period_start", "period_end"], name="targets_target_branch_period")]

    def __str__(self):
        return f"{self.branch} {self.get_kind_display()} {self.period_start:%d %b %Y}–{self.period_end:%d %b %Y}"

    @property
    def is_customers(self):
        return self.kind == TargetKind.CUSTOMERS


class TargetDay(models.Model):
    target = models.ForeignKey(Target, on_delete=models.CASCADE, related_name="days")
    date = models.DateField()
    planned_amount = models.DecimalField(**MONEY)
    required_amount = models.DecimalField(**MONEY, null=True, blank=True)   # frozen at the day's start

    class Meta:
        db_table = "targets_target_day"
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["target", "date"], name="targets_day_one_per_date")]

    def __str__(self):
        return f"{self.target} {self.date:%d}"
