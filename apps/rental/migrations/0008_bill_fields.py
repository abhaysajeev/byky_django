"""No bill before the ride ends (order_lifecycle_design.md 1 "Money", 4.2, 4.3).

- A line keeps its package and agreed base_fare from booking; overtime_amount
  and total_amount are filled when it is returned. rate becomes base_fare;
  amount, discount and tax_amount go (no line discount, VAT is on the order).
- The order's bill -- subtotal, discount, VAT, rounding, net_amount -- is
  filled at settle. total_amount becomes subtotal, card_discount_amount
  becomes discount_amount (with discount_claim and discount_percentage),
  total_tax becomes tax_amount, rounded_diff becomes rounding_adjustment;
  total_discount goes.
- An order still active has its bill blanked, and its lines' final amounts;
  completed and cancelled orders keep their figures.
- payment_status follows the order: pending while active, paid once settled,
  blank once cancelled. It and balance_due are dropped and re-added, because
  Postgres cannot change a computed column's formula in place.
"""

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Case, F, Value, When

MONEY = {"max_digits": 12, "decimal_places": 2}
PAID = F("amount_received") - F("amount_refunded")
BILL = ("subtotal", "discount_amount", "tax_percentage", "tax_amount", "rounding_adjustment", "net_amount")


def blank_open_bills(apps, schema_editor):
    apps.get_model("rental", "Order").objects.filter(status="active").update(**dict.fromkeys(BILL))
    apps.get_model("rental", "OrderItem").objects.filter(status="active").update(
        overtime_amount=None, total_amount=None,
    )


def nullable_money(**extra):
    return models.DecimalField(blank=True, null=True, **{**MONEY, **extra})


class Migration(migrations.Migration):

    dependencies = [
        ('discount', '0002_claim_order_cancelled'),
        ('rental', '0007_order_event'),
    ]

    operations = [
        # -- Order -------------------------------------------------------------------
        migrations.RemoveIndex(model_name='order', name='order_payment_57a3c1_idx'),
        migrations.RemoveField(model_name='order', name='payment_status'),
        migrations.RemoveField(model_name='order', name='balance_due'),
        migrations.RemoveConstraint(model_name='order', name='order_amounts_not_negative'),

        migrations.RenameField(model_name='order', old_name='total_amount', new_name='subtotal'),
        migrations.RenameField(model_name='order', old_name='card_discount_amount', new_name='discount_amount'),
        migrations.RenameField(model_name='order', old_name='total_tax', new_name='tax_amount'),
        migrations.RenameField(model_name='order', old_name='rounded_diff', new_name='rounding_adjustment'),
        migrations.RemoveField(model_name='order', name='total_discount'),

        migrations.AlterField(model_name='order', name='subtotal', field=nullable_money()),
        migrations.AlterField(model_name='order', name='discount_amount', field=nullable_money()),
        migrations.AlterField(
            model_name='order', name='tax_percentage', field=nullable_money(max_digits=5),
        ),
        migrations.AlterField(model_name='order', name='tax_amount', field=nullable_money()),
        migrations.AlterField(
            model_name='order', name='rounding_adjustment', field=nullable_money(max_digits=6),
        ),
        migrations.AlterField(model_name='order', name='net_amount', field=nullable_money()),
        migrations.AddField(
            model_name='order', name='discount_claim',
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='applied_to_order', to='discount.carddiscountclaim',
            ),
        ),
        migrations.AddField(
            model_name='order', name='discount_percentage', field=nullable_money(max_digits=5),
        ),

        # -- OrderItem -----------------------------------------------------------------
        migrations.RemoveConstraint(model_name='orderitem', name='order_item_amounts_not_negative'),
        migrations.RenameField(model_name='orderitem', old_name='rate', new_name='base_fare'),
        migrations.RemoveField(model_name='orderitem', name='amount'),
        migrations.RemoveField(model_name='orderitem', name='discount'),
        migrations.RemoveField(model_name='orderitem', name='tax_amount'),
        migrations.AlterField(model_name='orderitem', name='overtime_amount', field=nullable_money()),
        migrations.AlterField(model_name='orderitem', name='total_amount', field=nullable_money()),

        # -- Open orders have no bill yet ------------------------------------------------
        migrations.RunPython(blank_open_bills, migrations.RunPython.noop),
        # Run the foreign-key checks the update deferred, or Postgres refuses
        # the next ALTER TABLE ("pending trigger events").
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),

        # -- Computed columns and checks, on the new fields ---------------------------
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
                    When(status='active', then=Value('pending')),
                    When(status='completed', then=Value('paid')),
                    default=None,
                ),
                output_field=models.CharField(
                    choices=[('pending', 'Pending'), ('paid', 'Paid')], max_length=20, null=True,
                ),
            ),
        ),
        migrations.AddIndex(
            model_name='order', index=models.Index(fields=['payment_status'], name='order_payment_57a3c1_idx'),
        ),
        migrations.AddConstraint(
            model_name='order',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    subtotal__gte=0, discount_amount__gte=0, net_amount__gte=0,
                    amount_received__gte=0, amount_refunded__gte=0,
                ),
                name='order_amounts_not_negative',
            ),
        ),
        migrations.AddConstraint(
            model_name='orderitem',
            constraint=models.CheckConstraint(
                condition=models.Q(base_fare__gte=0, overtime_amount__gte=0, total_amount__gte=0),
                name='order_item_amounts_not_negative',
            ),
        ),
    ]
