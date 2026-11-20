"""The registry re-applied: Tracking's Status Board is now Status Tracking and
Readers is Reader CPU Approval (client, 20 Nov 2026 -- the old RMS names). Only
the names change; codes, URLs and grants stay."""

from django.db import migrations

from apps.portal.page_sync import sync


def apply_registry(apps, schema_editor):
    sync(apps)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0038_incentives_pages'),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
