"""Give a session a UUID key.

Sessions are transactions, not masters (design/00-findings.md section 9).

Written by hand because PostgreSQL cannot cast bigint to uuid, so the generated
AlterField fails. The table is empty and nothing references it, so the column is
replaced outright; `state_operations` tells Django the end state matches the
model.
"""

from django.db import migrations, models

import core.ids


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0007_recommend_and_presentation"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        "DELETE FROM app_session;",
                        "ALTER TABLE app_session DROP CONSTRAINT app_session_pkey;",
                        "ALTER TABLE app_session DROP COLUMN id;",
                        "ALTER TABLE app_session ADD COLUMN id uuid PRIMARY KEY;",
                    ],
                    reverse_sql=[
                        "DELETE FROM app_session;",
                        "ALTER TABLE app_session DROP CONSTRAINT app_session_pkey;",
                        "ALTER TABLE app_session DROP COLUMN id;",
                        "ALTER TABLE app_session ADD COLUMN id bigserial PRIMARY KEY;",
                    ],
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="appsession",
                    name="id",
                    field=models.UUIDField(
                        default=core.ids.uuid7, editable=False,
                        primary_key=True, serialize=False,
                    ),
                ),
            ],
        ),
    ]
