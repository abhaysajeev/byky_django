"""The registry re-applied: Rental gains Blocked Customers (owner, 3 Nov 2026).

Nothing is granted here, as in 0011: roles get the new page on the Privileges
screen or with `manage.py sync_role_pages`.
"""

from django.db import migrations

from apps.portal.page_sync import sync


def apply_registry(apps, schema_editor):
    sync(apps)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0029_sync_issues_for_companies'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
