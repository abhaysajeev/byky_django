"""A vehicle taken off an order is `removed`, not `cancelled`
(order_lifecycle_design.md P3): `cancelled` is kept for a line closed by the
whole order's cancel. A line already `cancelled` on an order still active can
only have been taken off, so it becomes `removed`.
"""

from django.db import migrations, models


def cancelled_to_removed(apps, schema_editor):
    apps.get_model("rental", "OrderItem").objects.filter(status="cancelled", order__status="active").update(
        status="removed",
    )


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0009_return_settle_invoice'),
    ]

    operations = [
        migrations.AlterField(
            model_name='orderitem',
            name='status',
            field=models.CharField(
                choices=[('active', 'Active'), ('returned', 'Returned'), ('replaced', 'Replaced'),
                         ('removed', 'Removed'), ('cancelled', 'Cancelled')],
                default='active', max_length=20,
            ),
        ),
        migrations.RunPython(cancelled_to_removed, migrations.RunPython.noop),
    ]
