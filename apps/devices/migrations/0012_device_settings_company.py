"""device_settings gets its own company column, and its two codes become
unique per company instead of globally.

`company` is a copy of `branch.company`, held only so the unique constraints
below can see it -- a constraint cannot reach across to `branch`. It is set
from the branch in `DeviceSettings.save()`, never by a caller, and the
composite foreign key at the end makes Postgres refuse a row whose copy ever
disagrees. The exact pattern `app_release_mapping.company` already uses
(devices/0007), for the same reason.

Added nullable, backfilled from the existing rows' own branches, then made
required -- the standard three-step shape for a NOT NULL column on a table
that already has rows. All 95 imported device_settings rows have a branch, so
every row backfills.
"""

import django.db.models.deletion
from django.db import migrations, models

COMPOSITE_FK = """
ALTER TABLE device_settings
    ADD CONSTRAINT device_settings_branch_company_fk
    FOREIGN KEY (branch_id, company_id)
    REFERENCES branch (id, company_id)
    DEFERRABLE INITIALLY DEFERRED;
"""

DROP_COMPOSITE_FK = """
ALTER TABLE device_settings
    DROP CONSTRAINT IF EXISTS device_settings_branch_company_fk;
"""

BACKFILL = """
UPDATE device_settings
   SET company_id = branch.company_id
  FROM branch
 WHERE branch.id = device_settings.branch_id;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0007_branch_code_per_company"),
        ("devices", "0011_device_settings_logo_bytes"),
    ]

    operations = [
        migrations.AddField(
            model_name="devicesettings",
            name="company",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="device_settings", to="company.company",
            ),
        ),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
        migrations.AlterField(
            model_name="devicesettings",
            name="company",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="device_settings", to="company.company",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="devicesettings",
            name="uniq_active_settings_code",
        ),
        migrations.RemoveConstraint(
            model_name="devicesettings",
            name="uniq_active_order_no_prefix",
        ),
        migrations.AddConstraint(
            model_name="devicesettings",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)),
                fields=("company", "settings_code"),
                name="uniq_active_settings_code_per_company",
                violation_error_message="Settings code already exists for this company.",
            ),
        ),
        migrations.AddConstraint(
            model_name="devicesettings",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)),
                fields=("company", "order_no_prefix"),
                name="uniq_active_order_no_prefix_per_company",
                violation_error_message="Another station of this company already uses this order prefix.",
            ),
        ),
        migrations.AddIndex(
            model_name="devicesettings",
            index=models.Index(fields=["company"], name="device_sett_company_6b3672_idx"),
        ),
        migrations.RunSQL(COMPOSITE_FK, DROP_COMPOSITE_FK),
    ]
