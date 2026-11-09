"""The registry re-applied: the Tracking module (design/07-tracking.md §7).

A new page is reachable by nobody until someone grants it, so it is granted to
the roles that can already change Device Mapping (readers are devices). Nothing
else is granted; the module stays empty until a reader is set up.
"""

from django.db import migrations

from apps.portal.page_sync import sync

NEW = ("tracking.status", "tracking.alerts", "tracking.readers", "tracking.antennas", "tracking.raw",
       "tracking.settings", "tracking.privileges")
FLAGS = ("create", "read", "update", "approve")


def apply_registry(apps, schema_editor):
    sync(apps)
    Page = apps.get_model("portal", "Page")
    RolePermission = apps.get_model("portal", "RolePermission")
    mappers = set(
        RolePermission.objects.filter(page__code="devices.device_mapping", can_update=True)
        .values_list("role_id", flat=True)
    )
    for page in Page.objects.filter(code__in=NEW):
        grants = {f"can_{flag}": True for flag in FLAGS if flag in page.actions}
        for role_id in mappers:
            RolePermission.objects.update_or_create(role_id=role_id, page=page, defaults=grants)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0034_cancelled_invoices_page'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
