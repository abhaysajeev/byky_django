"""Device-to-station mapping, with history, and the device status log.

The legacy kept both jobs in SfaDeviceMapping: which station a device belongs to
(FromDate/ToDate) and who was logged in on it (LoginTime/LogoutTime/UserID). The
second half is AppSession. This is the first.

The data migration opens a mapping row for every device that already carries a
branch, so the history table is never born empty beside a populated
device.branch. It is a no-op on today's table, which holds no rows.
"""

import django.db.models.deletion
from django.db import migrations, models


def open_mappings_for_existing_devices(apps, schema_editor):
    Device = apps.get_model("devices", "Device")
    DeviceMapping = apps.get_model("devices", "DeviceMapping")
    for device in Device.objects.filter(branch__isnull=False).order_by("pk"):
        started = device.approved_at or device.created_on
        DeviceMapping.objects.create(
            device=device,
            branch_id=device.branch_id,
            from_date=started.date(),
            created_by_id=device.approved_by_id,
        )


def drop_mappings(apps, schema_editor):
    apps.get_model("devices", "DeviceMapping").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0005_widen_location_short_code"),
        ("core", "0005_user_employee"),
        ("devices", "0003_device_rev5"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeviceMapping",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_on", models.DateTimeField(auto_now_add=True)),
                ("modified_on", models.DateTimeField(auto_now=True, null=True)),
                ("from_date", models.DateField()),
                ("to_date", models.DateField(blank=True, null=True)),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="device_mappings",
                        to="company.branch",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.user",
                    ),
                ),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="mappings",
                        to="devices.device",
                    ),
                ),
                (
                    "modified_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.user",
                    ),
                ),
            ],
            options={
                "db_table": "device_mapping",
                "ordering": ["-from_date", "-id"],
            },
        ),
        migrations.CreateModel(
            name="DeviceStatusLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_on", models.DateTimeField(auto_now_add=True)),
                ("modified_on", models.DateTimeField(auto_now=True, null=True)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("approved", "Approved"),
                            ("rejected", "Rejected"),
                            ("blocked", "Blocked"),
                            ("unblocked", "Unblocked"),
                            ("reconnected", "Reconnected after reinstall"),
                            ("replaced", "Replaced an earlier device"),
                            ("retired", "Retired"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "from_status",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("pending", "Awaiting approval"),
                            ("approved", "Approved"),
                            ("blocked", "Blocked"),
                            ("retired", "Retired"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "to_status",
                    models.CharField(
                        choices=[
                            ("pending", "Awaiting approval"),
                            ("approved", "Approved"),
                            ("blocked", "Blocked"),
                            ("retired", "Retired"),
                        ],
                        max_length=16,
                    ),
                ),
                ("reason", models.CharField(blank=True, max_length=100)),
                ("remarks", models.TextField(blank=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.user",
                    ),
                ),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="status_log",
                        to="devices.device",
                    ),
                ),
                (
                    "modified_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.user",
                    ),
                ),
                (
                    "related_device",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="devices.device",
                    ),
                ),
            ],
            options={
                "db_table": "device_status_log",
                "ordering": ["-created_on", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="devicemapping",
            index=models.Index(fields=["branch", "to_date"], name="device_mapp_branch__2db516_idx"),
        ),
        migrations.AddIndex(
            model_name="devicemapping",
            index=models.Index(fields=["device", "to_date"], name="device_mapp_device__c6ea0b_idx"),
        ),
        migrations.AddConstraint(
            model_name="devicemapping",
            constraint=models.UniqueConstraint(
                condition=models.Q(("to_date__isnull", True)),
                fields=("device",),
                name="uniq_open_mapping_per_device",
            ),
        ),
        migrations.AddConstraint(
            model_name="devicemapping",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("to_date__isnull", True),
                    ("to_date__gte", models.F("from_date")),
                    _connector="OR",
                ),
                name="device_mapping_dates_in_order",
            ),
        ),
        migrations.AddIndex(
            model_name="devicestatuslog",
            index=models.Index(fields=["device"], name="device_stat_device__9b3c6d_idx"),
        ),
        migrations.RunPython(open_mappings_for_existing_devices, drop_mappings),
    ]
