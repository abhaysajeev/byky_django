"""Retire the Station Address Mapping and Branch Department Mapping screens.

Both edited data that already has a home on Branch: the station's number,
address, coordinates and contact (design/02-company.md 3.4 folded the station
address into the branch), and the branch-to-department assignment, which is the
`departments` multiselect in the Branch drawer. Two screens editing the same
columns is a duplicate, not a feature.

The Page row is deactivated rather than deleted, so any RolePermission rows
pointing at it stay intact and the history of who could open it survives.
"""

from django.db import migrations

CODES = ["company.branch_address", "company.branch_department"]


def retire(apps, schema_editor):
    apps.get_model("portal", "Page").objects.filter(code__in=CODES).update(is_active=False)


def restore(apps, schema_editor):
    apps.get_model("portal", "Page").objects.filter(code__in=CODES).update(is_active=True)


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0005_role_system_scope"),
    ]

    operations = [migrations.RunPython(retire, restore)]
