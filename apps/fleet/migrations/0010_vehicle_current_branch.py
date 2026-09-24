"""vehicle.current_branch: where a vehicle is now.

Nullable -- every vehicle imported so far has no station yet, and a vehicle
can be at none (warehouse, in transit). Set by hand on the Vehicle screen until
Inventory Branch Mapping exists and keeps it current.

The composite foreign key makes Postgres refuse a branch of another company:
(current_branch_id, company_id) must match a branch's (id, company_id) --
uniq_branch_id_company (company 0007) is its target, the same pattern as
device_settings (devices 0012). MATCH SIMPLE skips it while current_branch_id
is empty.
"""

import django.db.models.deletion
from django.db import migrations, models

COMPOSITE_FK = """
ALTER TABLE vehicle
    ADD CONSTRAINT vehicle_current_branch_company_fk
    FOREIGN KEY (current_branch_id, company_id)
    REFERENCES branch (id, company_id)
    DEFERRABLE INITIALLY DEFERRED;
"""

DROP_COMPOSITE_FK = """
ALTER TABLE vehicle DROP CONSTRAINT IF EXISTS vehicle_current_branch_company_fk;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0008_weekday_monday_zero'),
        ('fleet', '0009_remove_vehicle_uniq_vehicle_rfid_epc_per_company_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='vehicle',
            name='current_branch',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='vehicles', to='company.branch', verbose_name='Current Branch'),
        ),
        migrations.RunSQL(COMPOSITE_FK, DROP_COMPOSITE_FK),
    ]
