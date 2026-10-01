"""Order and OrderItem rebuilt to order_lifecycle_design.md (section 4).

Altered in place rather than recreated, so orders already on QA -- and the
card-discount claims pointing at them -- survive:

- device_created_at becomes booked_at, paid_amount becomes amount_received
  (renames: the values carry over); amount_received / amount_refunded are then
  recomputed from the payment entries, which are the source of truth.
- paid_amount, balance_due and payment_status come back as columns the
  database computes.
- Dropped: invoice_no (never filled), synced_at (= created_on),
  number_of_vehicles (the count of lines), advance_amount (Advance payment
  entries), payment_mode (per payment entry).
- order_no becomes required and unique per company: a blank or repeated one is
  replaced by "X" + the start of the order's id before the key is added.
- OrderItem gains the audit columns, overtime_amount, and 12-digit money; its
  id no longer has a server default (the tablet sends it).
"""

from collections import defaultdict
from decimal import Decimal

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import Case, F, Q, Value, When

MONEY = {"max_digits": 12, "decimal_places": 2}
PAID = F("amount_received") - F("amount_refunded")


def fix_order_rows(apps, schema_editor):
    Order = apps.get_model("rental", "Order")
    seen = defaultdict(set)
    for order in Order.objects.select_related("customer").order_by("created_on"):
        changed = []
        if order.status == "payment_pending":
            order.status = "active"
            changed.append("status")
        if order.customer_id and order.customer_mobile != order.customer.mobile_full:
            order.customer_mobile = order.customer.mobile_full
            changed.append("customer_mobile")
        if not order.order_no or order.order_no in seen[order.company_id]:
            order.order_no = "X" + order.pk.hex[:24]
            changed.append("order_no")
        seen[order.company_id].add(order.order_no)
        if changed:
            order.save(update_fields=changed)


def recompute_received(apps, schema_editor):
    Order = apps.get_model("rental", "Order")
    Payment = apps.get_model("rental", "Payment")
    totals = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for order_id, kind, amount in Payment.objects.values_list("order_id", "kind", "amount"):
        totals[order_id][1 if kind == "refund" else 0] += amount
    for order in Order.objects.all():
        received, refunded = totals[order.pk]
        if (order.amount_received, order.amount_refunded) != (received, refunded):
            order.amount_received, order.amount_refunded = received, refunded
            order.save(update_fields=["amount_received", "amount_refunded"])


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0004_order_payment'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # -- Order: data first, while every old column is still there ------------
        migrations.RunPython(fix_order_rows, migrations.RunPython.noop),
        # Run the foreign-key checks the updates above deferred, or Postgres
        # refuses the next ALTER TABLE ("pending trigger events").
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
        migrations.RemoveConstraint(model_name='order', name='order_amounts_not_negative'),
        migrations.RenameField(model_name='order', old_name='device_created_at', new_name='booked_at'),
        # Straight after the rename: the old default ordering names the old column.
        migrations.AlterModelOptions(name='order', options={'ordering': ['-booked_at']}),
        migrations.RenameField(model_name='order', old_name='paid_amount', new_name='amount_received'),
        migrations.RemoveField(model_name='order', name='invoice_no'),
        migrations.RemoveField(model_name='order', name='synced_at'),
        migrations.RemoveField(model_name='order', name='number_of_vehicles'),
        migrations.RemoveField(model_name='order', name='advance_amount'),
        migrations.RemoveField(model_name='order', name='payment_mode'),
        migrations.AddField(
            model_name='order', name='amount_refunded', field=models.DecimalField(**MONEY, default=0),
        ),
        migrations.RunPython(recompute_received, migrations.RunPython.noop),
        # Run the foreign-key checks the updates above deferred, or Postgres
        # refuses the next ALTER TABLE ("pending trigger events").
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
        migrations.AddField(
            model_name='order', name='card_discount_amount', field=models.DecimalField(**MONEY, default=0),
        ),
        migrations.AddField(
            model_name='order', name='completed_at', field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order', name='cancelled_at', field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='order', name='customer',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name='orders', to='rental.customer',
            ),
        ),
        migrations.AlterField(model_name='order', name='order_no', field=models.CharField(max_length=30)),
        migrations.AlterField(
            model_name='order', name='status',
            field=models.CharField(
                choices=[('active', 'Active'), ('completed', 'Completed'), ('cancelled', 'Cancelled')],
                default='active', max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='order', name='paid_amount',
            field=models.GeneratedField(
                db_persist=True, expression=PAID, output_field=models.DecimalField(**MONEY),
            ),
        ),
        migrations.AddField(
            model_name='order', name='balance_due',
            field=models.GeneratedField(
                db_persist=True, expression=F('net_amount') - PAID, output_field=models.DecimalField(**MONEY),
            ),
        ),
        migrations.AddField(
            model_name='order', name='payment_status',
            field=models.GeneratedField(
                db_persist=True,
                expression=Case(
                    When(Q(amount_received__lte=F('amount_refunded'), net_amount__gt=0), then=Value('unpaid')),
                    When(Q(net_amount__gt=PAID), then=Value('partly_paid')),
                    default=Value('paid'),
                ),
                output_field=models.CharField(
                    choices=[('unpaid', 'Unpaid'), ('partly_paid', 'Partly Paid'), ('paid', 'Paid')],
                    max_length=20,
                ),
            ),
        ),
        migrations.AddIndex(
            model_name='order', index=models.Index(fields=['payment_status'], name='order_payment_57a3c1_idx'),
        ),
        migrations.AddIndex(
            model_name='order', index=models.Index(fields=['booked_at'], name='order_booked__56158c_idx'),
        ),
        migrations.AddConstraint(
            model_name='order',
            constraint=models.UniqueConstraint(
                fields=('company', 'order_no'), name='uniq_order_no_per_company',
                violation_error_message='That order number is already used.',
            ),
        ),
        migrations.AddConstraint(
            model_name='order',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ('amount_received__gte', 0), ('amount_refunded__gte', 0),
                    ('net_amount__gte', 0), ('total_amount__gte', 0),
                ),
                name='order_amounts_not_negative',
            ),
        ),

        # -- OrderItem ---------------------------------------------------------------
        migrations.RemoveConstraint(model_name='orderitem', name='order_item_amounts_not_negative'),
        migrations.RenameField(model_name='orderitem', old_name='remarks', new_name='reason'),
        migrations.AlterField(
            model_name='orderitem', name='id', field=models.UUIDField(editable=False, primary_key=True, serialize=False),
        ),
        migrations.AlterField(
            model_name='orderitem', name='status',
            field=models.CharField(
                choices=[('active', 'Active'), ('returned', 'Returned'), ('replaced', 'Replaced'),
                         ('cancelled', 'Cancelled')],
                default='active', max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='orderitem', name='created_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='orderitem', name='created_on',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='orderitem', name='modified_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='orderitem', name='modified_on', field=models.DateTimeField(auto_now=True, null=True),
        ),
        migrations.AddField(
            model_name='orderitem', name='overtime_amount', field=models.DecimalField(**MONEY, default=0),
        ),
        migrations.AlterField(model_name='orderitem', name='rate', field=models.DecimalField(**MONEY)),
        migrations.AlterField(model_name='orderitem', name='amount', field=models.DecimalField(**MONEY)),
        migrations.AlterField(
            model_name='orderitem', name='discount', field=models.DecimalField(**MONEY, default=0),
        ),
        migrations.AlterField(
            model_name='orderitem', name='tax_amount', field=models.DecimalField(**MONEY, default=0),
        ),
        migrations.AlterField(model_name='orderitem', name='total_amount', field=models.DecimalField(**MONEY)),
        migrations.AddConstraint(
            model_name='orderitem',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ('amount__gte', 0), ('overtime_amount__gte', 0), ('rate__gte', 0), ('total_amount__gte', 0),
                ),
                name='order_item_amounts_not_negative',
            ),
        ),
    ]
