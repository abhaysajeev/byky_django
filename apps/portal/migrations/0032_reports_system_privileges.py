"""The registry re-applied: Reports and Users & Roles gain a Privileges screen
(owner, 3 Nov 2026 -- company admins use custom roles, and these two modules
could not be granted from the web).

A Privileges page can only be granted from its own screen, so a new one would
be reachable by nobody. Each new page is therefore granted (read + update) to
the roles that may already change privileges on another module. Nothing else
is granted.
"""

from django.db import migrations

from apps.portal.page_sync import sync

NEW = ("reports.privileges", "system.privileges")


def apply_registry(apps, schema_editor):
    sync(apps)
    Page = apps.get_model("portal", "Page")
    RolePermission = apps.get_model("portal", "RolePermission")
    admins = set(
        RolePermission.objects.filter(page__code__endswith=".privileges", can_update=True)
        .exclude(page__code__in=NEW).values_list("role_id", flat=True)
    )
    for page in Page.objects.filter(code__in=NEW):
        for role_id in admins:
            RolePermission.objects.update_or_create(
                role_id=role_id, page=page, defaults={"can_read": True, "can_update": True})


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0031_configurations_page'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
