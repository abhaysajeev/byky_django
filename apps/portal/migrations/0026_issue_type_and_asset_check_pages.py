"""The registry re-applied: Inventory gains Issue Type and Asset Checks.

Nothing is granted here, as in 0011: roles are given the new screens on the
Privileges screen or with `manage.py sync_role_pages`.
"""

from django.db import migrations

from apps.portal.page_sync import sync


def apply_registry(apps, schema_editor):
    sync(apps)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0025_service_and_image_share_pages'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
