"""The registry re-applied: Rental gains its Credit Notes screen.

Nothing is granted here, as in 0011: roles are given the new screen on the
Privileges screen or with `manage.py sync_role_pages`.
"""

from django.db import migrations

from apps.portal.page_sync import sync


def apply_registry(apps, schema_editor):
    sync(apps)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0018_offer_page_to_package'),
        ('rental', '0011_credit_note'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
