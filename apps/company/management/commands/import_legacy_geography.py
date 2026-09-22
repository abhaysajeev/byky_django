"""Import the client's live Country and State rows.

    python manage.py import_legacy_geography --dry-run
    python manage.py import_legacy_geography --replace-stand-ins

Reads `data/legacy/core_country.json` and `core_state.json`, written from the
client's workbook by `tools/extract_live_masters.py`. The rules and the reasons
behind them are in `apps/company/legacy_import.py`.

**Dry run first.** It reports every row it would write, every row it would skip,
and every stand-in standing in the way, then rolls back.

Re-running is safe: rows are written by primary key, so a second run updates
what the first one inserted, and the stand-ins are gone by then.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.company import legacy_import as legacy
from apps.company.models import Country, State


class Command(BaseCommand):
    help = "Load Country and State from the client's live-data export."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change and roll back.",
        )
        parser.add_argument(
            "--replace-stand-ins", action="store_true",
            help=(
                "Deal with the rows created by hand while the screens were built: "
                "move anything pointing at them onto the real row, then delete or "
                "overwrite them. One of them holds a primary key the live data "
                "needs, so the import cannot finish without this."
            ),
        )

    def handle(self, *args, **options):
        country_rows, state_rows = legacy.countries(), legacy.states()
        standing = (
            [(Country, row, kind) for row, kind in legacy.stand_ins(Country, country_rows)]
            + [(State, row, kind) for row, kind in legacy.stand_ins(State, state_rows)]
        )

        if standing and not options["replace_stand_ins"]:
            self.describe(standing)
            raise CommandError(
                "These rows are in your database but not in the live export, and one "
                "of them holds a primary key the live data needs. Re-run with "
                "--replace-stand-ins to move their references and clear them. "
                "Nothing was written."
            )

        with transaction.atomic():
            # A stand-in that is about to be deleted may hold a unique value the
            # incoming row needs, so its values move aside before anything is
            # written (see legacy_import.park).
            for _, row, kind in standing:
                if kind == "orphan":
                    legacy.park(row)

            # Countries first: a state needs its country, and a stand-in's
            # replacement has to exist before anything is moved onto it.
            created_c, updated_c = legacy.write(Country, country_rows)

            # Then the states whose ids are free. A displaced id waits: writing
            # it now would overwrite the stand-in before its references move,
            # which is exactly the silent change this command exists to avoid.
            held = {row.pk for _, row, kind in standing
                    if isinstance(row, State) and kind == "displaced"}
            created_s, updated_s = legacy.write(
                State, [r for r in state_rows if r[0] not in held]
            )

            moved = []
            for model, row, kind in standing:
                target = legacy.replacement_for(row)
                if target is None:
                    raise CommandError(
                        f"{model._meta.model_name} {row.pk} ({row.short_code} / {row.name}) "
                        "has no recorded replacement, so its references have nowhere to "
                        "go. Nothing was written."
                    )
                # The pk is captured now: Model.delete() clears it, and the
                # report below still has to name the row that went.
                moved.append((row.pk, row, kind, target, legacy.repoint(row, target)))

            # Orphans go; displaced ids are freed by the write that follows.
            for _, row, kind, *_ in moved:
                if kind == "orphan":
                    row.delete()

            if held:
                extra = legacy.write(State, [r for r in state_rows if r[0] in held])
                created_s, updated_s = created_s, updated_s + extra[0] + extra[1]

            self.report(country_rows, state_rows, created_c, updated_c,
                        created_s, updated_s, moved)

            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("\nDry run -- nothing was written."))

    def describe(self, standing):
        self.stdout.write(self.style.WARNING(
            f"{len(standing)} row(s) are not in the live export:"
        ))
        for model, row, kind in standing:
            target = legacy.replacement_for(row)
            fate = "overwritten by the live row" if kind == "displaced" else "deleted"
            self.stdout.write(
                f"  {model._meta.model_name} {row.pk}: {row.short_code} / {row.name}"
                f"  -- references move to {target}, row {fate}"
            )

    def report(self, country_rows, state_rows, created_c, updated_c,
               created_s, updated_s, moved):
        self.stdout.write(self.style.SUCCESS(
            f"Countries: {created_c} created, {updated_c} updated."
        ))
        for pk, defaults, created_on, _ in country_rows:
            self.stdout.write(
                f"  {pk}  {defaults['short_code']:<8} {defaults['name']:<12} "
                f"since {created_on:%Y-%m-%d}"
            )

        self.stdout.write(self.style.SUCCESS(
            f"States: {created_s} created, {updated_s} updated."
        ))
        for pk, defaults, created_on, _ in state_rows:
            self.stdout.write(
                f"  {pk}  {defaults['short_code']:<8} {defaults['name']:<12} "
                f"country {defaults['country_id']}  since {created_on:%Y-%m-%d}"
            )

        for pk, row, kind, target, refs in moved:
            detail = ", ".join(refs) if refs else "nothing pointed at it"
            self.stdout.write(self.style.WARNING(
                f"Stand-in {row._meta.model_name} {pk} ({row.short_code} / {row.name}) "
                f"{'overwritten' if kind == 'displaced' else 'deleted'}; "
                f"references moved to {target}: {detail}"
            ))

        for line in legacy.skipped():
            self.stdout.write(f"Skipped {line}")
