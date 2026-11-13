"""A card and a manager discount on one bill (owner, 13 Nov 2026): their own
amounts on the order and the invoice. Bills settled before carry one
discount; it is copied into the matching pair by the invoice's source
(complimentary keeps using discount_amount alone)."""

from django.db import migrations, models
from django.db.models import F

PAIRS = {"card": ("card_discount_amount", "card_discount_percentage"),
         "manager": ("manager_discount_amount", "manager_discount_percentage")}


def fill(apps, schema_editor):
    Invoice = apps.get_model("rental", "Invoice")
    Order = apps.get_model("rental", "Order")
    for source, (amount, percentage) in PAIRS.items():
        Invoice.objects.filter(discount_source=source).update(
            **{amount: F("discount_amount"), percentage: F("discount_percentage")})
        Order.objects.filter(invoice__discount_source=source).update(
            **{amount: F("discount_amount"), percentage: F("discount_percentage")})


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0027_order_item_package_details'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='card_discount_amount',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=13, null=True),
        ),
        migrations.AddField(
            model_name='invoice',
            name='card_discount_percentage',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name='invoice',
            name='manager_discount_amount',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=13, null=True),
        ),
        migrations.AddField(
            model_name='invoice',
            name='manager_discount_percentage',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='card_discount_amount',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=13, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='card_discount_percentage',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='manager_discount_amount',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=13, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='manager_discount_percentage',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AlterField(
            model_name='invoice',
            name='discount_source',
            field=models.CharField(blank=True, choices=[('card', 'Card discount'), ('manager', 'Discount'), ('complimentary', 'Complimentary'), ('card_manager', 'Card + manager discount')], max_length=15),
        ),
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
