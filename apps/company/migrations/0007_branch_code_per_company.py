"""Branch Code becomes unique per company, not globally.

It was globally unique from before multi-company existed
(design/03-login.md section 9B.2, 20 Sep 2026). Two companies on one server
should each be free to use "AUH01"; nothing else in the schema needed that
uniqueness to be global -- a branch is always reached through its own company.

No data migration: one company today, so no existing pair of codes can
collide under the new, narrower rule.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("company", "0006_company_tax_discount_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="branch",
            name="short_code",
            field=models.CharField(max_length=10, verbose_name="Branch Code"),
        ),
        migrations.AddConstraint(
            model_name="branch",
            constraint=models.UniqueConstraint(
                fields=("company", "short_code"),
                name="uniq_branch_code_per_company",
                violation_error_message="A branch with this code already exists.",
            ),
        ),
        migrations.AddConstraint(
            model_name="branch",
            constraint=models.UniqueConstraint(
                fields=("id", "company"),
                name="uniq_branch_id_company",
            ),
        ),
    ]
