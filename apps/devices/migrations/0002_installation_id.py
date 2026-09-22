"""Rename device_uid to installation_id.

Alone in its own migration so the rename is unambiguous and the column keeps its
data and its unique index, rather than being dropped and recreated.

The name follows what the app calls it (design/registration/registration-api.md
section 2): the UUID the app generates on first launch and keeps in secure
storage.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("devices", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="device",
            old_name="device_uid",
            new_name="installation_id",
        ),
    ]
