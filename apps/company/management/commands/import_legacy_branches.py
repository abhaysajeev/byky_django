"""Import the client's live Locations and Branches.

    python manage.py import_legacy_branches --dry-run
    python manage.py import_legacy_branches

**Locations come with the branches, not separately.** `Branch.location` is a
required foreign key and every branch has one, so importing branches without
them is not a smaller step — it is a step that cannot complete. Country, State
and Company must already be in (`import_legacy_geography`,
`import_legacy_company`).

98 locations and 96 branches, UAE only. Kuwait's single location and branch
(Hilton Kuwait Resort) are reported and left out.

**Five branch fields stay empty**: `station_number`, `address`, `latitude`,
`longitude` and `contact_no`. They live in `CmsStationAddressMapping`, which is
not in this workbook — 85 of the 97 branches have a station number, an address
and a phone number there, and latitude/longitude are empty on every row even in
the legacy. `departments` is empty for the same reason: `CoreBranchDepartments`
is its own table and Department has not been imported.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.company import legacy_import as legacy
from apps.company.models import Branch, Company, Location, State


class Command(BaseCommand):
    help = "Load Location and Branch from the client's live-data export."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change and roll back.")
        parser.add_argument("--list", action="store_true",
                            help="Print every row, not just the totals.")

    def handle(self, *args, **options):
        location_rows, branch_rows = legacy.locations(), legacy.branches()

        self.require(State, {d["state_id"] for _, d, *_ in location_rows}, "state",
                     "import_legacy_geography")
        self.require(Company, {d["company_id"] for _, d, *_ in branch_rows}, "company",
                     "import_legacy_company")

        with transaction.atomic():
            created_l, updated_l = legacy.write(Location, location_rows)
            created_b, updated_b = legacy.write(Branch, branch_rows)

            self.stdout.write(self.style.SUCCESS(
                f"Locations: {created_l} created, {updated_l} updated."
            ))
            self.stdout.write(self.style.SUCCESS(
                f"Branches:  {created_b} created, {updated_b} updated."
            ))

            if options["list"]:
                for pk, defaults, *_ in branch_rows:
                    self.stdout.write(
                        f"  {pk:>3}  {defaults['short_code']:<14} {defaults['name']:<32} "
                        f"location {defaults['location_id']:<4} "
                        f"{'active' if defaults['is_active'] else 'INACTIVE'}"
                    )

            self.summarise(branch_rows)

            for line in legacy.skipped_structure():
                self.stdout.write(f"Skipped {line}")

            self.stdout.write(
                "\nstation_number, address, latitude, longitude and contact_no are "
                "empty: they live in CmsStationAddressMapping, which is not in this "
                "export."
            )

            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("\nDry run -- nothing was written."))

    def require(self, model, ids, label, command):
        """Refuse early rather than fail halfway through on a foreign key."""
        missing = sorted(ids - set(model.objects.filter(pk__in=ids).values_list("pk", flat=True)))
        if missing:
            raise CommandError(
                f"{label} {missing} do not exist yet. Run {command} first. "
                "Nothing was written."
            )

    def summarise(self, branch_rows):
        """The shape of what went in, so the run is checkable at a glance."""
        inactive = [d["name"] for _, d, *_ in branch_rows if not d["is_active"]]
        by_type = {}
        for _, defaults, *_ in branch_rows:
            by_type[defaults["branch_type"]] = by_type.get(defaults["branch_type"], 0) + 1

        self.stdout.write("  types: " + ", ".join(f"{k} {v}" for k, v in sorted(by_type.items())))
        self.stdout.write(
            "  flags: "
            f"{sum(1 for _, d, *_ in branch_rows if d['is_multi_device'])} multi-device, "
            f"{sum(1 for _, d, *_ in branch_rows if d['allows_test_ride'])} test ride, "
            f"{sum(1 for _, d, *_ in branch_rows if d['is_hotel'])} hotel, "
            f"{sum(1 for _, d, *_ in branch_rows if d['accepts_app_payment'])} app payment"
        )
        if inactive:
            self.stdout.write(f"  inactive: {', '.join(inactive)}")
