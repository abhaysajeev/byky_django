"""The registry re-applied: the ERPNext module (design/10-erpnext-integration.md §17).

A new page is reachable by nobody until someone grants it, so ERPNext Inbox and
its Privileges screen are granted -- every action they have -- to the roles that
can already change System Privileges (the administrators who hand out rights).
Anyone else is given them on the ERPNext Privileges screen.
"""

from django.db import migrations

from apps.portal.page_sync import sync

NEW = ("integrations.erpnext_inbox", "integrations.privileges")
FLAGS = ("create", "read", "update", "delete", "print", "approve")


def apply_registry(apps, schema_editor):
    sync(apps)
    Page = apps.get_model("portal", "Page")
    RolePermission = apps.get_model("portal", "RolePermission")
    admins = set(
        RolePermission.objects.filter(page__code="system.privileges", can_update=True)
        .values_list("role_id", flat=True)
    )
    for page in Page.objects.filter(code__in=NEW):
        grants = {f"can_{flag}": True for flag in FLAGS if flag in page.actions}
        for role_id in admins:
            RolePermission.objects.update_or_create(role_id=role_id, page=page, defaults=grants)


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0041_image_share_to_rental"),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
