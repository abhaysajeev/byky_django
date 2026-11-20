"""Incentive schemes and their runs (design/09-incentives.md section 5,
design/11-target-profiles.md P6-P7).

A scheme is formulas an admin maintains for one target profile; a run is one
station's period of that profile worked out from the duty roster and its
target, kept as lines so every figure can be explained. An approved run is
final and never moves.
"""

from django.db import models
from django.db.models import F, Q

from apps.rental.models import MONEY
from core.models import TimeStampedModel


class AppliesTo(models.TextChoices):
    PRIMARY = "primary", "Primary cashier"
    SECONDARY = "secondary", "Secondary cashier"
    LABOUR = "labour", "Labour"
    TIER2 = "tier2", "Secondary cashier + labour"
    CASHIERS = "cashiers", "Cashiers (primary + secondary)"
    EVERYONE = "everyone", "Everyone rostered"


# Which roles each "applies to" covers.
APPLIES = {
    AppliesTo.PRIMARY: {"primary"}, AppliesTo.SECONDARY: {"secondary"}, AppliesTo.LABOUR: {"labour"},
    AppliesTo.TIER2: {"secondary", "labour"}, AppliesTo.CASHIERS: {"primary", "secondary"},
    AppliesTo.EVERYONE: {"primary", "secondary", "labour"},
}


class Payout(models.TextChoices):
    """Which month a run's money belongs to (11 P7)."""

    PERIOD_END_MONTH = "period_end_month", "Month the period ends"
    FINANCIAL_YEAR_END = "financial_year_end", "Financial-year end"


class Scheme(TimeStampedModel):
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="incentive_schemes")
    # The target profile it pays on (11 P6): its stations, periods and target.
    profile = models.ForeignKey("targets.TargetProfile", on_delete=models.PROTECT, related_name="schemes")
    payout = models.CharField(max_length=20, choices=Payout.choices, default=Payout.PERIOD_END_MONTH)
    name = models.CharField(max_length=100)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    # Empty = every station on the profile; a station-specific scheme wins over an
    # all-stations one of the same profile.
    branches = models.ManyToManyField("company.Branch", blank=True, related_name="incentive_schemes")
    pays_when = models.CharField(max_length=500, default="achieved >= target")
    is_active = models.BooleanField(default=False)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        db_table = "incentives_scheme"
        ordering = ["-effective_from", "name"]
        constraints = [
            models.CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gte=F("effective_from")),
                                   name="incentives_scheme_dates"),
        ]

    def __str__(self):
        return self.name

    @property
    def is_used(self):
        """Used by an approved run: then it can't be edited or deleted, only copied."""
        return self.runs.filter(status=RunStatus.APPROVED).exists()


class SchemeVariable(models.Model):
    """A derived variable: `name = formula`, built from the catalogue and the
    rows above it."""

    scheme = models.ForeignKey(Scheme, on_delete=models.CASCADE, related_name="variables")
    position = models.PositiveSmallIntegerField(default=0)
    name = models.CharField(max_length=40)
    formula = models.CharField(max_length=500)

    class Meta:
        db_table = "incentives_scheme_variable"
        ordering = ["position", "pk"]
        constraints = [models.UniqueConstraint(fields=["scheme", "name"], name="incentives_variable_unique")]

    def __str__(self):
        return f"{self.name} = {self.formula}"


class SchemeComponent(models.Model):
    """One line of pay: who it applies to, and how much per working day."""

    scheme = models.ForeignKey(Scheme, on_delete=models.CASCADE, related_name="components")
    position = models.PositiveSmallIntegerField(default=0)
    name = models.CharField(max_length=60)
    applies_to = models.CharField(max_length=12, choices=AppliesTo.choices)
    formula = models.CharField(max_length=500)

    class Meta:
        db_table = "incentives_scheme_component"
        ordering = ["position", "pk"]

    def __str__(self):
        return f"{self.name}: {self.formula}"


class RunStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    APPROVED = "approved", "Approved"


class Run(TimeStampedModel):
    """One station's period of one profile: the snapshot it was worked out from,
    and its total; `payout_month` is the first day of the month it is paid in."""

    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="+")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="incentive_runs")
    profile = models.ForeignKey("targets.TargetProfile", on_delete=models.PROTECT, related_name="runs")
    period_start = models.DateField()
    period_end = models.DateField()
    payout_month = models.DateField()
    scheme = models.ForeignKey(Scheme, on_delete=models.PROTECT, related_name="runs")
    target_amount = models.DecimalField(**MONEY)
    achieved = models.DecimalField(**MONEY)
    work_days = models.PositiveSmallIntegerField(default=0)
    basis = models.DecimalField(**MONEY, default=0)
    total = models.DecimalField(**MONEY, default=0)
    status = models.CharField(max_length=10, choices=RunStatus.choices, default=RunStatus.DRAFT)
    calculated_at = models.DateTimeField()
    calculated_by = models.ForeignKey("core.User", null=True, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "incentives_run"
        ordering = ["-period_start", "branch__name"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "profile", "period_start"],
                                    name="incentives_run_one_per_profile_period"),
            models.CheckConstraint(condition=Q(status="draft") | Q(approved_at__isnull=False),
                                   name="incentives_run_approved_has_time"),
        ]

    def __str__(self):
        return f"{self.branch} {self.period_start:%d %b %Y}–{self.period_end:%d %b %Y}"

    @property
    def is_approved(self):
        return self.status == RunStatus.APPROVED


class Line(models.Model):
    """One employee, one working day, one component."""

    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="lines")
    employee = models.ForeignKey("crew.Employee", on_delete=models.PROTECT, related_name="incentive_lines")
    date = models.DateField()
    role = models.CharField(max_length=10)
    component = models.CharField(max_length=60)
    amount = models.DecimalField(**MONEY)
    inputs = models.JSONField(default=dict)          # the values used: "why this figure"

    class Meta:
        db_table = "incentives_line"
        ordering = ["date", "employee__first_name", "pk"]
        indexes = [models.Index(fields=["employee", "date"], name="incentives_line_employee_date")]

    def __str__(self):
        return f"{self.employee} {self.date} {self.component} {self.amount}"
