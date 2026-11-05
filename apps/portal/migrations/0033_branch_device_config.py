"""The registry re-applied: Devices gains Branch-Device Config (owner, 5 Nov
2026 -- For Rent / For Receive per tablet at a multi-device branch).

A new page is otherwise reachable by nobody until someone grants it, so it is
granted (read + update) to the roles that can already change Device Mapping.
Nothing else is granted.
"""

from django.db import migrations

from apps.portal.page_sync import sync

NEW = "devices.branch_device_config"


def apply_registry(apps, schema_editor):
    sync(apps)
    Page = apps.get_model("portal", "Page")
    RolePermission = apps.get_model("portal", "RolePermission")
    mappers = set(
        RolePermission.objects.filter(page__code="devices.device_mapping", can_update=True)
        .values_list("role_id", flat=True)
    )
    for page in Page.objects.filter(code=NEW):
        for role_id in mappers:
            RolePermission.objects.update_or_create(
                role_id=role_id, page=page, defaults={"can_read": True, "can_update": True})


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0032_reports_system_privileges'),
        ('devices', '0017_device_rent_receive_flags'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
