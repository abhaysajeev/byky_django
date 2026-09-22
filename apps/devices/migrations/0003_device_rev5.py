"""Bring `device` up to design/03-login.md revision 5.

Adds the identifiers the registration API actually exchanges -- the numeric
device_registration_id, the platform and its id, the model -- plus the two
self-references behind reinstall and replacement, and the push token that lets
an approval reach the tablet.

device_registration_id runs off its own sequence rather than the primary key.
It is the number printed on a waiting screen and read down a phone line, so it
must not move if the key ever does. It starts at 1000, so the first support call
is "device 1000" rather than "device 1".
"""

import django.db.models.deletion
from django.db import migrations, models

CREATE_SEQUENCE = "CREATE SEQUENCE IF NOT EXISTS device_registration_seq START 1000"
DROP_SEQUENCE = "DROP SEQUENCE IF EXISTS device_registration_seq"


def backfill_registration_ids(apps, schema_editor):
    """Give existing devices a registration number.

    A no-op on an empty table, but a developer with local rows must not meet an
    IntegrityError when the column turns NOT NULL and unique below.
    """
    Device = apps.get_model("devices", "Device")
    connection = schema_editor.connection
    for device in Device.objects.filter(device_registration_id__isnull=True).order_by("pk"):
        with connection.cursor() as cursor:
            cursor.execute("SELECT nextval('device_registration_seq')")
            device.device_registration_id = cursor.fetchone()[0]
        device.save(update_fields=["device_registration_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0005_widen_location_short_code"),
        ("core", "0005_user_employee"),
        ("devices", "0002_installation_id"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_SEQUENCE, reverse_sql=DROP_SEQUENCE),
        migrations.AddField(
            model_name="device",
            name="device_model",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="device",
            name="platform",
            field=models.CharField(
                choices=[("android", "Android"), ("ios", "iOS")], default="android", max_length=8
            ),
        ),
        migrations.AddField(
            model_name="device",
            name="platform_id",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="device",
            name="push_token",
            field=models.CharField(blank=True, max_length=512),
        ),
        migrations.AddField(
            model_name="device",
            name="reconnect_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="reconnect_requests",
                to="devices.device",
            ),
        ),
        migrations.AddField(
            model_name="device",
            name="replaced_device",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="replaced_by",
                to="devices.device",
            ),
        ),
        # Added nullable, backfilled, then tightened -- so the migration is
        # correct on a table that already holds rows.
        migrations.AddField(
            model_name="device",
            name="device_registration_id",
            field=models.BigIntegerField(editable=False, null=True),
        ),
        migrations.RunPython(backfill_registration_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="device",
            name="device_registration_id",
            field=models.BigIntegerField(
                db_default=models.Func(
                    models.Value("device_registration_seq"), function="nextval"
                ),
                editable=False,
                unique=True,
            ),
        ),
        migrations.AlterModelOptions(
            name="device",
            options={"ordering": ["name", "device_registration_id"]},
        ),
        migrations.AddIndex(
            model_name="device",
            index=models.Index(fields=["status"], name="device_status_6321a6_idx"),
        ),
        migrations.AddIndex(
            model_name="device",
            index=models.Index(
                fields=["platform_id", "channel", "company"], name="device_platfor_1fd0d8_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="device",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("channel", "operator"), _negated=True),
                    models.Q(("status", "approved"), _negated=True),
                    ("branch__isnull", False),
                    _connector="OR",
                ),
                name="device_operator_needs_branch",
            ),
        ),
        migrations.AddConstraint(
            model_name="device",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    ("approved_at__isnull", False),
                    _connector="OR",
                ),
                name="device_approved_has_approval_time",
            ),
        ),
    ]
