"""Move every existing target under a profile (design/11-target-profiles.md §8).

Per company and kind: one active monthly profile ("Monthly sales" /
"Monthly customers") spanning the first to the last month that has targets.
Each station gets one assignment, at its latest target's amount; every
existing target is linked to it, and marked as edited on its own where its
amount differs -- so later edits to the assignment never rewrite it. No figure
changes: the targets, their days and their frozen amounts are untouched.
"""

from django.db import migrations

NAMES = {"sales": "Monthly sales", "customers": "Monthly customers"}


def forwards(apps, schema_editor):
    Target = apps.get_model("targets", "Target")
    Profile = apps.get_model("targets", "TargetProfile")
    Assignment = apps.get_model("targets", "TargetProfileBranch")
    groups = (Target.objects.filter(profile__isnull=True).values_list("company_id", "kind")
              .distinct().order_by())
    for company_id, kind in groups:
        targets = list(Target.objects.filter(company_id=company_id, kind=kind, profile__isnull=True)
                       .order_by("branch_id", "period_start"))
        name = NAMES.get(kind, kind.title())
        taken = set(Profile.objects.filter(company_id=company_id).values_list("name", flat=True))
        while name in taken:
            name += " (moved)"
        first = min(t.period_start for t in targets)
        last = max(t.period_end for t in targets)
        earliest = min(targets, key=lambda t: (t.created_on is None, t.created_on))
        profile = Profile.objects.create(
            company_id=company_id, name=name, metric=kind, period_type="monthly", starts_on=first,
            ends_on=last, status="active", remarks="Created when targets moved to profiles.",
            created_by_id=earliest.created_by_id, modified_by_id=earliest.modified_by_id)
        latest = {}
        for t in targets:
            latest[t.branch_id] = t                                   # ordered by period_start
        for branch_id, t in latest.items():
            Assignment.objects.create(profile=profile, branch_id=branch_id, amount=t.amount, active=True)
        for t in targets:
            t.profile_id = profile.pk
            t.period_type = "monthly"
            t.amount_overridden = t.amount != latest[t.branch_id].amount
        Target.objects.bulk_update(targets, ["profile", "period_type", "amount_overridden"])


def backwards(apps, schema_editor):
    Target = apps.get_model("targets", "Target")
    Profile = apps.get_model("targets", "TargetProfile")
    Target.objects.update(profile=None, amount_overridden=False)
    Profile.objects.filter(remarks="Created when targets moved to profiles.").delete()


class Migration(migrations.Migration):

    dependencies = [("targets", "0003_target_profiles")]

    operations = [migrations.RunPython(forwards, backwards)]
