"""Vehicle.identifier: VH-B-00001 becomes VB0001.

Only the text changes. identifier_no -- the number from the sequence -- is
untouched, so every vehicle keeps its place (VH-B-00042 is now VB0042) and new
vehicles carry on from the same sequence. Past 9999 it grows (VB10000).

Postgres cannot change a generated column's expression, so the column is
dropped and added again; the database recomputes it for every row.
"""

import django.db.models.functions.comparison
import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fleet', '0011_vehicle_identifier'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='vehicle',
            name='identifier',
        ),
        migrations.AddField(
            model_name='vehicle',
            name='identifier',
            field=models.GeneratedField(db_persist=True, expression=django.db.models.functions.text.Concat(models.Value('VB'), models.Case(models.When(identifier_no__lt=10000, then=django.db.models.functions.text.LPad(django.db.models.functions.comparison.Cast('identifier_no', models.CharField()), 4, models.Value('0'))), default=django.db.models.functions.comparison.Cast('identifier_no', models.CharField()))), output_field=models.CharField(max_length=30), unique=True, verbose_name='Vehicle Identifier'),
        ),
    ]
