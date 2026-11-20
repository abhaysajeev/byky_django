"""Every existing scheme and run pays on the company's monthly sales profile
(design/11-target-profiles.md §8): a run's money stays in its own month.

A company with schemes or runs but no sales targets yet gets the profile made
here (draft, the current month), so nothing is left without one."""

import datetime

from django.db import migrations


def _sales_profile(apps, company_id, user_id, day):
    Profile = apps.get_model("targets", "TargetProfile")
    found = (Profile.objects.filter(company_id=company_id, metric="sales", period_type="monthly")
             .order_by("pk").first())
    if found:
        return found
    name = "Monthly sales"
    while Profile.objects.filter(company_id=company_id, name=name).exists():
        name += " (moved)"
    first = day.replace(day=1)
    nxt = (first + datetime.timedelta(days=32)).replace(day=1)
    return Profile.objects.create(
        company_id=company_id, name=name, metric="sales", period_type="monthly", starts_on=first,
        ends_on=nxt - datetime.timedelta(days=1), status="draft",
        remarks="Created when targets moved to profiles.", created_by_id=user_id, modified_by_id=user_id)


def forwards(apps, schema_editor):
    Scheme = apps.get_model("incentives", "Scheme")
    Run = apps.get_model("incentives", "Run")
    for scheme in Scheme.objects.filter(profile__isnull=True):
        scheme.profile = _sales_profile(apps, scheme.company_id, scheme.created_by_id, scheme.effective_from)
        scheme.payout = "period_end_month"
        scheme.save(update_fields=["profile", "payout"])
    for run in Run.objects.filter(profile__isnull=True).select_related("scheme"):
        run.profile_id = run.scheme.profile_id
        run.payout_month = run.period_start.replace(day=1)
        run.save(update_fields=["profile", "payout_month"])


def backwards(apps, schema_editor):
    apps.get_model("incentives", "Run").objects.update(profile=None, payout_month=None)
    apps.get_model("incentives", "Scheme").objects.update(profile=None)


class Migration(migrations.Migration):

    dependencies = [
        ("incentives", "0004_scheme_run_profile"),
        ("targets", "0004_move_targets_to_profiles"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
