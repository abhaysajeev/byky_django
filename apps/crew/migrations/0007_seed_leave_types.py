"""The four leave types the employee app mockup shows, for every company that
exists now. A company made later adds its own on the Leave Type screen."""

from django.db import migrations

LEAVE_TYPES = [
    ("ANNUAL", "Annual Leave"),
    ("SICK", "Sick Leave"),
    ("EMERGENCY", "Emergency Leave"),
    ("CASUAL", "Casual Leave"),
]
APPROVED = 1


def seed(apps, schema_editor):
    Company = apps.get_model("company", "Company")
    LeaveType = apps.get_model("crew", "LeaveType")
    for company in Company.objects.all():
        for code, name in LEAVE_TYPES:
            LeaveType.objects.get_or_create(
                company=company, code=code,
                defaults={"name": name, "approval_status": APPROVED, "is_active": True},
            )


class Migration(migrations.Migration):

    dependencies = [
        ("crew", "0006_leave_request"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
