"""Departments belong to a company.

The first pass made a department's code and name unique system-wide, which
multi-company makes wrong: two companies must each be able to run an
"Operations" department. Revision 5 of design/02-company.md.

Written by hand rather than generated, because the generated version asks for a
one-off default for the new required column. The table is empty, so no backfill
is needed.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="department",
            name="short_code",
            field=models.CharField(max_length=10),
        ),
        migrations.AlterField(
            model_name="department",
            name="name",
            field=models.CharField(max_length=100),
        ),
        migrations.AddField(
            model_name="department",
            name="company",
            field=models.ForeignKey(
                default=None,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="departments",
                to="company.company",
            ),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="department",
            name="company",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="departments",
                to="company.company",
            ),
        ),
        migrations.AddConstraint(
            model_name="department",
            constraint=models.UniqueConstraint(
                fields=("company", "short_code"), name="uniq_department_code_per_company"
            ),
        ),
        migrations.AddConstraint(
            model_name="department",
            constraint=models.UniqueConstraint(
                fields=("company", "name"), name="uniq_department_name_per_company"
            ),
        ),
        migrations.AddIndex(
            model_name="department",
            index=models.Index(fields=["company"], name="department_company_1cc0ea_idx"),
        ),
    ]
