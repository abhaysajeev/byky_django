"""Import the client's live Company row.

    python manage.py import_legacy_company --dry-run
    python manage.py import_legacy_company --replace-stand-ins

Country and State must be imported first — the company's registered address
points at them (`manage.py import_legacy_geography`).

The rules are in `apps/company/legacy_import.py`. Two are worth knowing before
reading the output:

* **The name is base64 in the live database** and is decoded here, so the row
  reads `BY KY SPORT & LEISURE EQUIPMENT RENTAL & TRADING LLC` rather than
  `QlkgS1kgU1BPUlQ...`.
* **`Email` is NULL and `Website` has no scheme.** Both are imported as they
  are. Those fields are required/validated on the form, not in the database,
  and correcting data on the way in is how an import starts disagreeing with
  its source.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.company import legacy_import as legacy
from apps.company.models import Company, Country, State


class Command(BaseCommand):
    help = "Load Company from the client's live-data export."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change and roll back.")
        parser.add_argument(
            "--replace-stand-ins", action="store_true",
            help=(
                "Move references off the company row created during the build and "
                "delete it. It holds the right short code at the wrong id, which "
                "CoreBranch.CompanyID points at."
            ),
        )

    def handle(self, *args, **options):
        rows = legacy.companies()

        missing = [
            f"country {defaults['country_id']}"
            for _, defaults, *_ in rows
            if not Country.objects.filter(pk=defaults["country_id"]).exists()
        ] + [
            f"state {defaults['state_id']}"
            for _, defaults, *_ in rows
            if not State.objects.filter(pk=defaults["state_id"]).exists()
        ]
        if missing:
            raise CommandError(
                "The company's registered address points at rows that do not exist "
                f"yet ({', '.join(missing)}). Run import_legacy_geography first. "
                "Nothing was written."
            )

        standing = legacy.stand_ins(Company, rows)
        if standing and not options["replace_stand_ins"]:
            self.stdout.write(self.style.WARNING(
                f"{len(standing)} company row(s) are not in the live export:"
            ))
            for row, kind in standing:
                self.stdout.write(
                    f"  company {row.pk}: {row.short_code} / {row.name}"
                    f"  -- references move to {legacy.replacement_for(row)}, "
                    f"row {'overwritten' if kind == 'displaced' else 'deleted'}"
                )
            raise CommandError(
                "Re-run with --replace-stand-ins to move their references and clear "
                "them. Nothing was written."
            )

        with transaction.atomic():
            # The stand-in holds "BYKY", and so does the live row at a different
            # id. Its unique values move aside first, or the insert collides
            # before the stand-in can be dealt with at all.
            for row, kind in standing:
                if kind == "orphan":
                    legacy.park(row)

            created, updated = legacy.write(Company, rows)

            moved = []
            for row, kind in standing:
                target = legacy.replacement_for(row)
                if target is None:
                    raise CommandError(
                        f"Company {row.pk} ({row.short_code}) has no recorded "
                        "replacement, so its references have nowhere to go. "
                        "Nothing was written."
                    )
                moved.append((row.pk, row, kind, target, legacy.repoint(row, target)))
            for _, row, kind, *_ in moved:
                if kind == "orphan":
                    row.delete()

            self.stdout.write(self.style.SUCCESS(
                f"Company: {created} created, {updated} updated."
            ))
            for pk, defaults, created_on, _ in rows:
                self.stdout.write(
                    f"  {pk}  {defaults['short_code']}  {defaults['name']}\n"
                    f"      {defaults['city']}, country {defaults['country_id']}, "
                    f"state {defaults['state_id']}  since {created_on:%Y-%m-%d}"
                )
                for field in ("email", "website"):
                    value = defaults[field]
                    if not value:
                        self.stdout.write(f"      {field}: empty in the live data")

            for pk, row, kind, target, refs in moved:
                detail = ", ".join(refs) if refs else "nothing pointed at it"
                self.stdout.write(self.style.WARNING(
                    f"Stand-in company {pk} ({row.short_code} / {row.name}) "
                    f"{'overwritten' if kind == 'displaced' else 'deleted'}; "
                    f"references moved to {target}: {detail}"
                ))

            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("\nDry run -- nothing was written."))
