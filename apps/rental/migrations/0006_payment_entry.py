"""Payment rebuilt as a payment entry (order_lifecycle_design.md 4.4).

- kind `rental` becomes `settlement` (money collected when the bill is settled).
- collected_at becomes paid_at: a rename, the values carry over. It is the
  tablet's time from here on.
- reference_date becomes a date (it is a card slip's or cheque's date).
- id loses its server default: the tablet sends it.
"""

from django.db import migrations, models


def rental_to_settlement(apps, schema_editor):
    apps.get_model("rental", "Payment").objects.filter(kind="rental").update(kind="settlement")


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0005_order_lifecycle'),
    ]

    operations = [
        migrations.RunPython(rental_to_settlement, migrations.RunPython.noop),
        # Run the foreign-key checks the update deferred, or Postgres refuses
        # the next ALTER TABLE ("pending trigger events").
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
        migrations.RenameField(model_name='payment', old_name='collected_at', new_name='paid_at'),
        # Straight after the rename: the old default ordering names the old column.
        migrations.AlterModelOptions(name='payment', options={'ordering': ['-paid_at']}),
        migrations.AlterField(
            model_name='payment', name='kind',
            field=models.CharField(
                choices=[('advance', 'Advance'), ('settlement', 'Settlement'), ('refund', 'Refund')], max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name='payment', name='reference_date', field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='payment', name='id',
            field=models.UUIDField(editable=False, primary_key=True, serialize=False),
        ),
        migrations.AlterField(
            model_name='payment', name='amount', field=models.DecimalField(decimal_places=2, max_digits=12),
        ),
        migrations.AddIndex(
            model_name='payment', index=models.Index(fields=['paid_at'], name='payment_paid_at_a3dd0c_idx'),
        ),
        migrations.AddIndex(
            model_name='payment', index=models.Index(fields=['mode'], name='payment_mode_id_4a1088_idx'),
        ),
    ]
