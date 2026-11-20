"""Target Profiles replaces Set Targets (design/11-target-profiles.md §8).

Every role keeps exactly the rights it had on Set Targets, now on Target
Profiles; Target Branch Mapping gets read, and update when Set Targets had
create or update. Set Targets stays listed as retired, with its grants, as
the record of who could open it.
"""

from django.db import migrations

from apps.portal.page_sync import sync

ACTIONS = ("create", "read", "update", "delete")


def apply_registry(apps, schema_editor):
    sync(apps)
    Page = apps.get_model("portal", "Page")
    RolePermission = apps.get_model("portal", "RolePermission")
    profiles = Page.objects.filter(code="targets.profiles").first()
    if profiles is None:
        return
    mapping = Page.objects.filter(code="targets.mapping").first()
    for grant in RolePermission.objects.filter(page__code="targets.manage"):
        flags = {f"can_{a}": getattr(grant, f"can_{a}") for a in ACTIONS}
        if any(flags.values()):
            RolePermission.objects.update_or_create(role_id=grant.role_id, page=profiles, defaults=flags)
            if mapping is not None:
                RolePermission.objects.update_or_create(
                    role_id=grant.role_id, page=mapping,
                    defaults={"can_read": True, "can_update": grant.can_create or grant.can_update})


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0038_incentives_pages'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
