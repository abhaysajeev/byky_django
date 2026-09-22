"""Drop `device.branch`: DeviceMapping is the only record of the station.

The column was a denormalised copy of the open mapping row, which meant two
writers for one fact. At 84 stations the join it saved is not worth the risk of
the two disagreeing, so the mapping table is now the single source and
`Device.current_branch` reads it.

What goes with it: `device_operator_needs_branch`. A check constraint cannot
read another table, so "an approved operator device must have a station" moves
to the approval service -- which is where the multi-device rule already had to
live, for exactly the same reason.

The rescue below should find nothing: migration 0004 already opened a mapping
for every device that carried a branch. It runs anyway, because dropping a
column is the one moment a forgotten row becomes unrecoverable.
"""

from django.db import migrations


def rescue_unmapped_branches(apps, schema_editor):
    Device = apps.get_model("devices", "Device")
    DeviceMapping = apps.get_model("devices", "DeviceMapping")
    mapped = set(
        DeviceMapping.objects.filter(to_date__isnull=True).values_list("device_id", flat=True)
    )
    for device in Device.objects.filter(branch__isnull=False).exclude(pk__in=mapped):
        started = device.approved_at or device.created_on
        DeviceMapping.objects.create(
            device=device,
            branch_id=device.branch_id,
            from_date=started.date(),
            created_by_id=device.approved_by_id,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("devices", "0004_device_mapping"),
    ]

    operations = [
        migrations.RunPython(rescue_unmapped_branches, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="device",
            name="device_operator_needs_branch",
        ),
        migrations.RemoveIndex(
            model_name="device",
            name="device_branch__c372fd_idx",
        ),
        migrations.RemoveField(
            model_name="device",
            name="branch",
        ),
    ]
