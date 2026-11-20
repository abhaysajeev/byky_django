"""The registry re-applied: the Incentives module (design/09-incentives.md 8).

Roles that can read Targets' Overview get read on every Incentives page;
roles that can update Company Configurations get every action.
"""

from django.db import migrations

from apps.portal.page_sync import sync

NEW = ("incentives.calculate", "incentives.employees", "incentives.schemes", "incentives.privileges")
FLAGS = ("create", "read", "update", "delete", "approve", "print")


def apply_registry(apps, schema_editor):
    sync(apps)
    Page = apps.get_model("portal", "Page")
    RolePermission = apps.get_model("portal", "RolePermission")
    readers = set(RolePermission.objects.filter(page__code="targets.overview", can_read=True)
                  .values_list("role_id", flat=True))
    admins = set(RolePermission.objects.filter(page__code="company.configurations", can_update=True)
                 .values_list("role_id", flat=True))
    for page in Page.objects.filter(code__in=NEW):
        for role_id in readers | admins:
            if role_id in admins:
                grants = {f"can_{flag}": True for flag in FLAGS if flag in page.actions}
            else:
                grants = {"can_read": True}
            RolePermission.objects.update_or_create(role_id=role_id, page=page, defaults=grants)


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0037_targets_pages"),
        ("incentives", "0002_standard_scheme"),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
