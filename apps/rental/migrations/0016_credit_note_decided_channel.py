"""Credit notes record where they were decided -- the web screen or the
manager app. Every decision so far was made on the web (the manager app could
not decide credit notes), so those rows are back-filled `web`; a request the
tablet cancelled itself has no decider and stays blank."""

from django.db import migrations, models


def backfill(apps, schema_editor):
    CreditNote = apps.get_model("rental", "CreditNote")
    CreditNote.objects.filter(decided_by__isnull=False).update(decided_channel="web")


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0015_order_branch_booked_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='creditnote',
            name='decided_channel',
            field=models.CharField(blank=True, choices=[('web', 'Web'), ('manager', 'Manager app')], max_length=10),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
