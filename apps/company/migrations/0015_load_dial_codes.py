"""Fill the dial-code table from the bundled file (apps/company/dial_codes.py),
so every database has the list after its next deploy. Re-runnable through
`manage.py load_dial_codes`."""

from django.db import migrations

from apps.company import dial_codes


def load(apps, schema_editor):
    dial_codes.load(apps.get_model("company", "DialCode"), dial_codes.read_file())


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0014_dial_code"),
    ]

    operations = [
        migrations.RunPython(load, migrations.RunPython.noop),
    ]
