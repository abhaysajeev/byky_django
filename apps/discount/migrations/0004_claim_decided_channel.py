"""Card-discount claims record where they were decided -- the web screen or
the manager app. Every approve / reject so far was made on the web, so those
rows are back-filled `web`; a claim the system cancelled at billing stays
blank."""

from django.db import migrations, models


def backfill(apps, schema_editor):
    CardDiscountClaim = apps.get_model("discount", "CardDiscountClaim")
    CardDiscountClaim.objects.filter(status__in=["approved", "rejected"]).update(decided_channel="web")


class Migration(migrations.Migration):

    dependencies = [
        ('discount', '0003_money_three_decimals'),
    ]

    operations = [
        migrations.AddField(
            model_name='carddiscountclaim',
            name='decided_channel',
            field=models.CharField(blank=True, choices=[('web', 'Web'), ('manager', 'Manager app')], max_length=10),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
