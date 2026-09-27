"""Vehicle.identifier: VH-B-00001, an internal number no one types or edits.

Existing vehicles are numbered in the order the user chose: category, then
vehicle type, then vehicle name -- a number-aware sort, so "MO 2" comes before
"MO 10" -- then vehicle code and id to settle ties. The sequence then carries
on from the last number, and every later insert takes the next one.
"""

import re

import django.db.models.functions.comparison
import django.db.models.functions.text
from django.db import migrations, models

SEQUENCE = "vehicle_identifier_seq"


def natural(text):
    """'MO 10' -> ['mo ', 10, ''] so numbers inside names sort as numbers."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text or "")]


def number_existing(apps, schema_editor):
    Vehicle = apps.get_model("fleet", "Vehicle")
    vehicles = Vehicle.objects.select_related("vehicle_type__category").only(
        "pk", "vehicle_code", "vehicle_name",
        "vehicle_type__vehicle_type_name", "vehicle_type__category__category_name",
    )
    ordered = sorted(vehicles, key=lambda v: (
        natural(v.vehicle_type.category.category_name), natural(v.vehicle_type.vehicle_type_name),
        natural(v.vehicle_name), natural(v.vehicle_code), v.pk,
    ))
    for number, vehicle in enumerate(ordered, start=1):
        vehicle.identifier_no = number
    Vehicle.objects.bulk_update(ordered, ["identifier_no"], batch_size=1000)
    with schema_editor.connection.cursor() as cursor:
        # is_called=false with 1 on an empty table: the first vehicle gets 1.
        cursor.execute("SELECT setval(%s, %s, %s)", [SEQUENCE, max(len(ordered), 1), bool(ordered)])


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0010_vehicle_current_branch'),
    ]

    operations = [
        migrations.RunSQL(f"CREATE SEQUENCE {SEQUENCE} START 1", f"DROP SEQUENCE {SEQUENCE}"),
        migrations.AddField(
            model_name='vehicle',
            name='identifier_no',
            field=models.BigIntegerField(null=True, editable=False),
        ),
        migrations.RunPython(number_existing, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='vehicle',
            name='identifier_no',
            field=models.BigIntegerField(db_default=models.Func(models.Value(SEQUENCE), function='nextval'), editable=False, unique=True),
        ),
        migrations.AddField(
            model_name='vehicle',
            name='identifier',
            field=models.GeneratedField(db_persist=True, expression=django.db.models.functions.text.Concat(models.Value('VH-B-'), models.Case(models.When(identifier_no__lt=100000, then=django.db.models.functions.text.LPad(django.db.models.functions.comparison.Cast('identifier_no', models.CharField()), 5, models.Value('0'))), default=django.db.models.functions.comparison.Cast('identifier_no', models.CharField()))), output_field=models.CharField(max_length=30), unique=True, verbose_name='Vehicle Identifier'),
        ),
        migrations.AlterModelOptions(
            name='vehicle',
            options={'ordering': ['identifier_no']},
        ),
    ]
