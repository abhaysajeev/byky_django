"""payment_status gains `balance_due` (owner, 27 Nov 2026).

A bill may now settle with part of it unpaid: the order shows "Balance due"
instead of "Paid". Postgres cannot change a generated column's expression, so
the column (and its index) is dropped and added back -- it is recomputed from
the row, and every bill settled so far was paid in full, so all stay `paid`.
"""

from django.db import migrations, models
from django.db.models import Case, F, Q, Value, When


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0030_suspense_entry'),
    ]

    operations = [
        migrations.RemoveIndex(model_name='order', name='order_payment_57a3c1_idx'),
        migrations.RemoveField(model_name='order', name='payment_status'),
        migrations.AddField(
            model_name='order', name='payment_status',
            field=models.GeneratedField(
                db_persist=True,
                expression=Case(
                    When(status='active', then=Value('pending')),
                    When(Q(status='completed', net_amount__gt=F('amount_received') - F('amount_refunded')),
                         then=Value('balance_due')),
                    When(status='completed', then=Value('paid')),
                    default=None,
                ),
                output_field=models.CharField(
                    choices=[('pending', 'Pending'), ('paid', 'Paid'), ('balance_due', 'Balance due')],
                    max_length=20, null=True,
                ),
            ),
        ),
        migrations.AddIndex(
            model_name='order', index=models.Index(fields=['payment_status'], name='order_payment_57a3c1_idx'),
        ),
    ]
