"""The registry re-applied: Raw Readings is for system administrators only
(owner, 9 Nov 2026). Nothing is granted or taken away; a company user is refused
by the page's system_only flag whatever their role ticks."""

from django.db import migrations

from apps.portal.page_sync import sync


def apply_registry(apps, schema_editor):
    sync(apps)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0035_tracking_pages'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
