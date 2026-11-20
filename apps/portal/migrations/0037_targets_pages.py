"""The registry re-applied: the Targets module (design/08-targets.md §7).

Granted to the roles that hold Company Configurations: read there gives read on
every Targets page; update there also gives Set Targets' create, update and
delete. Admins adjust it on the
module's Privileges screen.
"""

from django.db import migrations

from apps.portal.page_sync import sync

NEW = ("targets.overview", "targets.daily", "targets.manage", "targets.privileges")


def apply_registry(apps, schema_editor):
    sync(apps)
    Page = apps.get_model("portal", "Page")
    RolePermission = apps.get_model("portal", "RolePermission")
    held = RolePermission.objects.filter(page__code="company.configurations", can_read=True)
    for grant in held:
        for page in Page.objects.filter(code__in=NEW):
            flags = {"can_read": True}
            if grant.can_update:
                flags.update({f"can_{a}": True for a in ("create", "update", "delete") if a in page.actions})
            RolePermission.objects.update_or_create(role_id=grant.role_id, page=page, defaults=flags)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0036_tracking_raw_system_only'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
