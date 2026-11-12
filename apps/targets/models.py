"""Station targets (design/08-targets.md).

A target is one station, one period, one amount. Its days carry the planned
amount (the month split evenly, stored once) and the required amount (frozen
at that business day's start). What was achieved is never stored: it is
always worked out from the orders (apps/targets/services.py).
"""

from django.db import models
from django.db.models import F, Q

from apps.rental.models import MONEY
from core.models import TimeStampedModel


class Target(TimeStampedModel):
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="+")
    branch = models.ForeignKey("company.Branch", on_delete=models.PROTECT, related_name="targets")
    period_start = models.DateField()           # first business day of the period
    period_end = models.DateField()             # last business day, inclusive
    amount = models.DecimalField(**MONEY)
    remarks = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "targets_target"
        ordering = ["branch__name", "period_start"]
        constraints = [
            models.CheckConstraint(condition=Q(period_end__gte=F("period_start")), name="targets_target_period"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="targets_target_amount"),
            models.UniqueConstraint(fields=["branch", "period_start"], name="targets_target_one_per_period"),
        ]
        indexes = [models.Index(fields=["company", "period_start"], name="targets_target_company_period")]

    def __str__(self):
        return f"{self.branch} {self.period_start:%b %Y}"


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
