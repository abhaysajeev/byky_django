"""Every screen this release ships, from the page registry.

Replaces the per-module seed migrations (0004, 0010) as the way new screens
arrive. Those stay where they are as applied history; this re-asserts the same
rows and adds the ones they never had -- the System module (Roles, Users) and
each module's Privileges screen.

It reads apps/portal/page_registry.py as it is now, on purpose. See the note in
apps/portal/page_sync.py: these rows describe the software, not the customer's
data, so there is no earlier version of them worth preserving.

Nothing is granted here. `manage.py sync_role_pages` or the privilege screen is
how a role gets access (design/rbac.md section 7).
"""

from django.db import migrations

from apps.portal.page_sync import sync


def apply_registry(apps, schema_editor):
    sync(apps)


def noop(apps, schema_editor):
    """Nothing to undo: the rows this writes are re-asserted by the next run,
    and deleting them would take their RolePermission rows with them."""


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0010_seed_crew_pages"),
    ]

    operations = [migrations.RunPython(apply_registry, noop)]
