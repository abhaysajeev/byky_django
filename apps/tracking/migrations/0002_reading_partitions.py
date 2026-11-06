"""tracking_reading: the raw reads, partitioned by day (design §4).

Django does not manage this table (Reading.Meta.managed = False); it is made
here, with today + 3 days of partitions and the default partition. The engine
keeps making them ahead and drops old ones (apps/tracking/partitions.py).

The grants give the gateway's own role (tracking_writer, made once per
environment from deploy/tracking-role.sql) only what it needs, and only on
tracking tables. If the role does not exist yet nothing is granted; the role
SQL repeats the same grants when it is run.
"""

from django.db import migrations
from django.utils import timezone

from apps.tracking import partitions

GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracking_writer') THEN
    GRANT SELECT, INSERT ON tracking_reading, tracking_reading_key, tracking_raw_frame TO tracking_writer;
    GRANT SELECT, INSERT, UPDATE ON tracking_tag_last_seen TO tracking_writer;
    GRANT SELECT, INSERT, UPDATE ON tracking_reader TO tracking_writer;
    GRANT SELECT ON tracking_antenna, tracking_reader_assignment TO tracking_writer;
    GRANT USAGE ON SEQUENCE tracking_reader_id_seq, tracking_raw_frame_id_seq TO tracking_writer;
  END IF;
END $$;
"""


def first_partitions(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        partitions.ensure(cursor, timezone.now().date(), ahead=3)


class Migration(migrations.Migration):

    dependencies = [("tracking", "0001_initial")]

    operations = [
        migrations.RunSQL(partitions.CREATE_PARENT, partitions.DROP_PARENT),
        migrations.RunPython(first_partitions, migrations.RunPython.noop),
        migrations.RunSQL(GRANTS, migrations.RunSQL.noop),
    ]
