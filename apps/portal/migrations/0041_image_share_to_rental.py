"""Image Sharing moves from Inventory to the Rental sidebar (client, 22 Nov
2026). Same page code, so every role keeps its grant; only the module moves."""

from django.db import migrations

from apps.portal.page_sync import sync


def apply_registry(apps, schema_editor):
    sync(apps)


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0040_target_profiles_page"),
    ]

    operations = [
        migrations.RunPython(apply_registry, migrations.RunPython.noop),
    ]
