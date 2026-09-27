"""Nested special prices and seasons.

Rules of one kind in one list, and seasons in one fare, may now sit one
inside another -- the innermost wins (apps.fare.pricing). Only crossing
(partly overlapping) and identical windows are refused, since those are the
two cases with no single innermost.

An exclusion constraint cannot say "overlap unless nested", so the old ones
are replaced by constraint triggers that do the same job:

  fare_rule_no_crossing    same fare and season list, same kind, a shared day
                           (every day; weekdays &&; the same date), windows
                           that overlap and are identical or not nested.
  fare_season_no_crossing  same fare, date ranges that overlap and are
                           identical or not nested.

Both are DEFERRABLE INITIALLY IMMEDIATE, like the constraints they replace:
services.save_fare defers them while it rewrites a fare's rows. A deferred
check fires after later statements may have changed or deleted the row, so
each re-reads its row by id. Each also locks the parent fare row, so two
transactions adding rows to one fare cannot both pass -- save_fare locks it
already; this covers any other writer.

Also caps every minutes field at 1440 (one day).
"""

from django.db import migrations, models

TRIGGERS = """
CREATE FUNCTION fare_rule_no_crossing() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    r fare_rule%ROWTYPE;
BEGIN
    SELECT * INTO r FROM fare_rule WHERE id = NEW.id;
    IF NOT FOUND THEN
        RETURN NULL;                                   -- deleted since
    END IF;
    PERFORM 1 FROM fare WHERE id = r.fare_id FOR NO KEY UPDATE;
    IF EXISTS (
        SELECT 1 FROM fare_rule o
        WHERE o.id <> r.id
          AND o.fare_id = r.fare_id
          AND o.season_id IS NOT DISTINCT FROM r.season_id
          AND o.kind = r.kind
          AND (r.kind = 'every_day'
               OR (r.kind = 'selected_days' AND o.weekdays && r.weekdays)
               OR (r.kind = 'single_date' AND o.on_date = r.on_date))
          AND o.start_minute < r.end_minute AND r.start_minute < o.end_minute
          AND ((o.start_minute = r.start_minute AND o.end_minute = r.end_minute)
               OR NOT ((o.start_minute <= r.start_minute AND r.end_minute <= o.end_minute)
                       OR (r.start_minute <= o.start_minute AND o.end_minute <= r.end_minute)))
    ) THEN
        RAISE EXCEPTION 'Two prices of the same kind cross or cover exactly the same time.'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'fare_rule_no_crossing';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER fare_rule_no_crossing
    AFTER INSERT OR UPDATE ON fare_rule
    DEFERRABLE INITIALLY IMMEDIATE
    FOR EACH ROW EXECUTE FUNCTION fare_rule_no_crossing();

CREATE FUNCTION fare_season_no_crossing() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    s fare_season%ROWTYPE;
BEGIN
    SELECT * INTO s FROM fare_season WHERE id = NEW.id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    PERFORM 1 FROM fare WHERE id = s.fare_id FOR NO KEY UPDATE;
    IF EXISTS (
        SELECT 1 FROM fare_season o
        WHERE o.id <> s.id
          AND o.fare_id = s.fare_id
          AND o.start_date <= s.end_date AND s.start_date <= o.end_date
          AND ((o.start_date = s.start_date AND o.end_date = s.end_date)
               OR NOT ((o.start_date <= s.start_date AND s.end_date <= o.end_date)
                       OR (s.start_date <= o.start_date AND o.end_date <= s.end_date)))
    ) THEN
        RAISE EXCEPTION 'Two seasons cross or cover exactly the same dates.'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'fare_season_no_crossing';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER fare_season_no_crossing
    AFTER INSERT OR UPDATE ON fare_season
    DEFERRABLE INITIALLY IMMEDIATE
    FOR EACH ROW EXECUTE FUNCTION fare_season_no_crossing();
"""

DROP_TRIGGERS = """
DROP TRIGGER fare_season_no_crossing ON fare_season;
DROP FUNCTION fare_season_no_crossing();
DROP TRIGGER fare_rule_no_crossing ON fare_rule;
DROP FUNCTION fare_rule_no_crossing();
"""

MINUTES_MAX = models.Q(
    ("concurrent_grace_minutes__lte", 1440), ("concurrent_interval_minutes__lte", 1440), ("grace_minutes__lte", 1440),
)


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0008_weekday_monday_zero'),
        ('core', '0005_user_employee'),
        ('fare', '0001_initial'),
        ('fleet', '0010_vehicle_current_branch'),
    ]

    operations = [
        migrations.RemoveConstraint(model_name='farerule', name='fare_rule_every_day_fare'),
        migrations.RemoveConstraint(model_name='farerule', name='fare_rule_every_day_season'),
        migrations.RemoveConstraint(model_name='farerule', name='fare_rule_days_fare'),
        migrations.RemoveConstraint(model_name='farerule', name='fare_rule_days_season'),
        migrations.RemoveConstraint(model_name='farerule', name='fare_rule_single_date'),
        migrations.RemoveConstraint(model_name='fareseason', name='fare_season_no_overlap'),
        migrations.AddConstraint(
            model_name='fare',
            constraint=models.CheckConstraint(condition=models.Q(('package_minutes__lte', 1440)),
                                              name='fare_package_max'),
        ),
        migrations.AddConstraint(
            model_name='fare',
            constraint=models.CheckConstraint(condition=MINUTES_MAX, name='fare_minutes_max'),
        ),
        migrations.AddConstraint(
            model_name='farerule',
            constraint=models.CheckConstraint(condition=MINUTES_MAX, name='fare_rule_minutes_max'),
        ),
        migrations.AddConstraint(
            model_name='fareseason',
            constraint=models.CheckConstraint(condition=MINUTES_MAX, name='fare_season_minutes_max'),
        ),
        migrations.RunSQL(TRIGGERS, DROP_TRIGGERS),
    ]
