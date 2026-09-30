from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0002_customer_sync_id_mobile_full'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='customer',
            constraint=models.UniqueConstraint(fields=('mobile_full',), name='uniq_customer_mobile_full', violation_error_message='A customer with this phone number already exists.'),
        ),
    ]
