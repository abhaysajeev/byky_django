"""Page.system_only, and the registry re-applied: the Logs module (Device
Requests, Error Logs) arrives with its pages marked system-only.

Nothing is granted here, as in 0011: a system role is given the new screens by
`manage.py sync_role_pages` (design/rbac.md section 7).
"""

from django.db import migrations, models

from apps.portal.page_sync import sync


def apply_registry(apps, schema_editor):
    sync(apps)


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0012_drop_session_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='page',
            name='system_only',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
