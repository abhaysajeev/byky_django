"""Dashboard Privileges (owner, 27 Nov 2026).

The dashboard is now granted per role. So that nobody loses it on deploy, every
active role is given Dashboard access; an administrator then unticks roles on
the new Dashboard Privileges screen. That screen itself is granted to the roles
that can already change System Privileges (the administrators who hand out
rights), as for every new page.
"""

from django.db import migrations

from apps.portal.page_sync import sync


def apply_registry(apps, schema_editor):
    sync(apps)
    Page = apps.get_model("portal", "Page")
    Role = apps.get_model("portal", "Role")
    RolePermission = apps.get_model("portal", "RolePermission")

    dashboard = Page.objects.get(code="general.dashboard")
    for role_id in Role.objects.filter(is_active=True).values_list("pk", flat=True):
        RolePermission.objects.update_or_create(role_id=role_id, page=dashboard, defaults={"can_read": True})

    screen = Page.objects.get(code="general.privileges")
    admins = set(
        RolePermission.objects.filter(page__code="system.privileges", can_update=True)
        .values_list("role_id", flat=True)
    )
    for role_id in admins:
        RolePermission.objects.update_or_create(
            role_id=role_id, page=screen, defaults={"can_read": True, "can_update": True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0043_suspense_page"),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
