"""crew 0006 seeds the employee app mockup's four leave types for every
company that exists, once -- running it again adds nothing."""

import importlib

from django.apps import apps as django_apps

from apps.crew.models import LeaveType
from apps.crew.tests.test_attendance import world  # noqa: F401  (fixture)

seed = importlib.import_module("apps.crew.migrations.0006_seed_leave_types").seed


def test_every_company_gets_the_four_types_once(world):  # noqa: F811
    LeaveType.objects.all().delete()

    seed(django_apps, None)
    seed(django_apps, None)

    for company in (world["company"], world["other"]):
        types = LeaveType.objects.filter(company=company).order_by("code")
        assert [(t.code, t.name, t.is_active) for t in types] == [
            ("ANNUAL", "Annual Leave", True), ("CASUAL", "Casual Leave", True),
            ("EMERGENCY", "Emergency Leave", True), ("SICK", "Sick Leave", True),
        ]
