"""Packages lose Inventory Type and the "Each" promotion type (owner, 19 Oct
2026). Inventory Type had one value (Vehicle Type) and did nothing; "Each"
meant the same as "Amount" -- a discount amount per item -- so a package saved
as Each becomes Amount, with its item values unchanged."""

from django.db import migrations, models


def each_to_amount(apps, schema_editor):
    Package = apps.get_model("fare", "Package")
    Package.objects.filter(promotion_type="each").update(promotion_type="amount")


class Migration(migrations.Migration):

    dependencies = [
        ('fare', '0005_money_three_decimals'),
    ]

    operations = [
        # Back is a no-op: "Amount" stays a valid type, and nothing can tell
        # which packages were once "Each".
        migrations.RunPython(each_to_amount, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='package',
            name='inventory_type',
        ),
        migrations.AlterField(
            model_name='package',
            name='promotion_type',
            field=models.CharField(choices=[('quantity', 'Quantity'), ('amount', 'Amount'), ('percentage', 'Percentage'), ('package_price', 'Package Price')], max_length=15, verbose_name='Promotion Type'),
        ),
    ]
