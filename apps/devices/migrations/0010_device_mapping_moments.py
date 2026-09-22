"""Device mapping dates become moments (date and time).

Mapping is stamped when it happens, as the legacy did (DeviceMappingModels.cs:235,
DeviceMappingController.cs:315), instead of a date the admin typed. A plain
AlterField would cast each existing date to midnight *UTC* -- 04:00 in Dubai --
so the database step is written out: existing dates become midnight in the
company's timezone, which is what a date meant here.
"""

from django.db import migrations, models

TO_MOMENT = """
ALTER TABLE device_mapping
    ALTER COLUMN from_date TYPE timestamp with time zone
        USING (from_date::timestamp AT TIME ZONE 'Asia/Dubai'),
    ALTER COLUMN to_date TYPE timestamp with time zone
        USING (to_date::timestamp AT TIME ZONE 'Asia/Dubai');
"""

TO_DATE = """
ALTER TABLE device_mapping
    ALTER COLUMN from_date TYPE date USING ((from_date AT TIME ZONE 'Asia/Dubai')::date),
    ALTER COLUMN to_date TYPE date USING ((to_date AT TIME ZONE 'Asia/Dubai')::date);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("devices", "0009_bill_continuity"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(TO_MOMENT, TO_DATE)],
            state_operations=[
                migrations.AlterField(
                    model_name="devicemapping",
                    name="from_date",
                    field=models.DateTimeField(),
                ),
                migrations.AlterField(
                    model_name="devicemapping",
                    name="to_date",
                    field=models.DateTimeField(blank=True, null=True),
                ),
            ],
        ),
    ]
