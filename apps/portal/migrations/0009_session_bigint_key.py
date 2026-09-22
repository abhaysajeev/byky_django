"""Put the session key back to an integer.

Sessions are created by the server, on the server, and never leave it, so they
gain nothing from a UUID -- the reasons for one are offline creation, several
writers, or exposure outside the system. UUID keys stay where they were asked
for: the RMS transaction tables (design/00-findings.md section 9).

By hand for the same reason 0008 was: PostgreSQL cannot cast between uuid and
bigint. The table is empty and nothing references it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0008_session_uuid_key"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        "DELETE FROM app_session;",
                        "ALTER TABLE app_session DROP CONSTRAINT app_session_pkey;",
                        "ALTER TABLE app_session DROP COLUMN id;",
                        "ALTER TABLE app_session ADD COLUMN id bigserial PRIMARY KEY;",
                    ],
                    reverse_sql=[
                        "DELETE FROM app_session;",
                        "ALTER TABLE app_session DROP CONSTRAINT app_session_pkey;",
                        "ALTER TABLE app_session DROP COLUMN id;",
                        "ALTER TABLE app_session ADD COLUMN id uuid PRIMARY KEY;",
                    ],
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="appsession",
                    name="id",
                    field=models.BigAutoField(
                        auto_created=True, primary_key=True,
                        serialize=False, verbose_name="ID",
                    ),
                ),
            ],
        ),
    ]
