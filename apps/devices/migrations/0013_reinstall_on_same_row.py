# A reinstall now waits on its own device's row (owner, 9 Oct 2026) instead of
# a separate reconnect-request row. Open requests are folded in: the newest one
# per device becomes that device's waiting install, and the request rows go --
# the same thing reconnect_device did with them on confirmation.

from django.db import migrations, models


def fold_reconnect_requests(apps, schema_editor):
    Device = apps.get_model("devices", "Device")
    requests = (Device.objects.filter(status="pending", reconnect_of__isnull=False)
                .select_related("reconnect_of").order_by("reconnect_of_id", "-created_on"))
    seen = set()
    for request_row in requests:
        original = request_row.reconnect_of
        if original.pk not in seen and original.status in ("approved", "blocked", "pending"):
            seen.add(original.pk)
            moved = {
                "installation_id": request_row.installation_id,
                "model": request_row.device_model, "push": request_row.push_token,
                "since": request_row.last_seen_at or request_row.created_on,
            }
            request_row.delete()
            if original.status == "pending":
                # Never approved: the newer install simply becomes its own.
                original.installation_id = moved["installation_id"]
                original.device_model = moved["model"] or original.device_model
                original.push_token = moved["push"] or original.push_token
            else:
                original.pending_installation_id = moved["installation_id"]
                original.pending_since = moved["since"]
                original.pending_device_model = moved["model"]
                original.pending_push_token = moved["push"]
            original.save()
        else:
            request_row.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0010_money_three_decimals'),
        ('core', '0005_user_employee'),
        ('devices', '0012_device_settings_company'),
    ]

    operations = [
        migrations.AddField(
            model_name='device',
            name='pending_device_model',
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name='device',
            name='pending_installation_id',
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='device',
            name='pending_push_token',
            field=models.CharField(blank=True, max_length=512),
        ),
        migrations.AddField(
            model_name='device',
            name='pending_since',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='device',
            name='rejected_installation_id',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.RunPython(fold_reconnect_requests, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='device',
            name='reconnect_of',
        ),
        migrations.AddConstraint(
            model_name='device',
            constraint=models.CheckConstraint(condition=models.Q(('pending_installation_id__isnull', True), models.Q(models.Q(('pending_installation_id', models.F('installation_id')), _negated=True), ('pending_since__isnull', False)), _connector='OR'), name='device_waiting_install_is_another'),
        ),
    ]
