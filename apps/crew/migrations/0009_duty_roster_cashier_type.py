"""Primary / Secondary cashier on the duty roster (owner, 13 Nov 2026).

Every Working day worked as Cashier becomes Primary. A day saved before roles
had no role and read as the employee's designation (duty.role_of); such a day
gets that role written down. Should a branch ever have two cashiers on one day,
the first saved stays Primary and the rest become Secondary, so the
one-Primary rule below can always be added.
"""

from django.db import migrations, models

WORKING, CASHIER, PRIMARY, SECONDARY = "working", "cashier", "primary", "secondary"


def fill(apps, schema_editor):
    """Role and cashier type are written together, so a row is never a Cashier
    day without a type, whether or not the constraints already exist."""
    from django.db.models import Q

    DutyRoster = apps.get_model("crew", "DutyRoster")
    cashier_days = DutyRoster.objects.filter(
        Q(role=CASHIER) | Q(role="", employee__designation__roster_category=CASHIER), day_type=WORKING)
    seen = set()
    primaries, secondaries = [], []
    for pk, branch_id, date in cashier_days.order_by("created_on", "id").values_list("pk", "branch_id", "date"):
        (secondaries if (branch_id, date) in seen else primaries).append(pk)
        seen.add((branch_id, date))
    DutyRoster.objects.filter(pk__in=primaries).update(role=CASHIER, cashier_type=PRIMARY)
    DutyRoster.objects.filter(pk__in=secondaries).update(role=CASHIER, cashier_type=SECONDARY)


class Migration(migrations.Migration):

    dependencies = [
        ("crew", "0008_duty_roster_role"),
    ]

    operations = [
        migrations.AddField(
            model_name="dutyroster",
            name="cashier_type",
            field=models.CharField(blank=True, choices=[("primary", "Primary"), ("secondary", "Secondary")],
                                   default="", max_length=10),
        ),
        migrations.RunPython(fill, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="dutyroster",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("cashier_type__in", ["primary", "secondary"]), ("day_type", "working"),
                             ("role", "cashier")),
                    models.Q(models.Q(("day_type", "working"), ("role", "cashier"), _negated=True),
                             ("cashier_type", "")),
                    _connector="OR",
                ),
                name="duty_roster_cashier_type_only_for_cashier_days",
            ),
        ),
        migrations.AddConstraint(
            model_name="dutyroster",
            constraint=models.UniqueConstraint(
                condition=models.Q(("cashier_type", "primary")), fields=("branch", "date"),
                name="uniq_duty_roster_primary_cashier_per_branch_day",
                violation_error_message="This branch already has a primary cashier that day.",
            ),
        ),
    ]
