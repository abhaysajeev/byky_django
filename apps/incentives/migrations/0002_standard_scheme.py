"""The client's rule as a starting scheme (design/09-incentives.md 4.3), one
per company, INACTIVE until an admin reviews and switches it on:

  pays when       achieved >= target
  Primary cashier (Primary cashier)          basis * 1%
  Tier 2 share    (Secondary cashier + labour) basis * 1% / max(tier2, 2)
"""

import datetime

from django.db import migrations


def seed(apps, schema_editor):
    Company = apps.get_model("company", "Company")
    Scheme = apps.get_model("incentives", "Scheme")
    Component = apps.get_model("incentives", "SchemeComponent")
    for company in Company.objects.all():
        if Scheme.objects.filter(company=company).exists():
            continue
        scheme = Scheme.objects.create(
            company=company, name="Standard incentive", effective_from=datetime.date(2026, 10, 1),
            pays_when="achieved >= target", is_active=False,
            notes="Primary cashier 1%; secondary cashier and labour share 1%, at most 0.5% each.")
        Component.objects.create(scheme=scheme, position=1, name="Primary cashier", applies_to="primary",
                                 formula="basis * 1%")
        Component.objects.create(scheme=scheme, position=2, name="Tier 2 share", applies_to="tier2",
                                 formula="basis * 1% / max(tier2, 2)")


class Migration(migrations.Migration):

    dependencies = [
        ("incentives", "0001_initial"),
        ("company", "0017_companyconfig_target_period"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
