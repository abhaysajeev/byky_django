"""Cashier Test Time Slot -> Staff Test Time Slot (owner, 28 Oct 2026: test
ride modes are Customer and Staff). A rename: the stored minutes are kept."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('devices', '0013_reinstall_on_same_row'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='devicesettings',
            name='device_settings_test_minutes_positive',
        ),
        migrations.RenameField(
            model_name='devicesettings',
            old_name='cashier_test_minutes',
            new_name='staff_test_minutes',
        ),
        migrations.AlterField(
            model_name='devicesettings',
            name='staff_test_minutes',
            field=models.PositiveSmallIntegerField(default=5, verbose_name='Staff Test Time Slot (Min)'),
        ),
        migrations.AddConstraint(
            model_name='devicesettings',
            constraint=models.CheckConstraint(
                condition=models.Q(('customer_test_minutes__gt', 0), ('staff_test_minutes__gt', 0)),
                name='device_settings_test_minutes_positive',
            ),
        ),
    ]
