"""The registry re-applied: Sync Issues is no longer system-only.

A company user whose role has its Read tick now sees their own company's
parked calls; Replay and Dismiss stay with system admins (apps/sync/views).
Nothing is granted here, as in 0011.
"""

from django.db import migrations

from apps.portal.page_sync import sync


def apply_registry(apps, schema_editor):
    sync(apps)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0028_report_pages'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
