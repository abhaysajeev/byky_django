"""Import the client's live device settings, logos included.

    python manage.py import_legacy_device_settings --dry-run
    python manage.py import_legacy_device_settings

95 stations: the receipt wording, the printer's habits, the order-number prefix
and the station's own bitmap. Branches must already be in
(`import_legacy_branches`) -- each settings row belongs to one.

The logo is written as bytes exactly as the legacy stored it, and
`LogoChangedOn` is kept: a tablet re-downloads its logo only when that moment
moves past its last sync, so stamping them all with now would send 95 stations
to fetch a picture they already have.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.company.legacy_import import write
from apps.company.models import Branch
from apps.devices import legacy_import as legacy
from apps.devices.models import DeviceSettings


class Command(BaseCommand):
    help = "Load DeviceSettings from the client's live-data export."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change and roll back.")
        parser.add_argument("--list", action="store_true",
                            help="Print every row, not just the totals.")

    def handle(self, *args, **options):
        branch_ids = set(Branch.objects.values_list("pk", flat=True))
        if not branch_ids:
            raise CommandError(
                "No branches exist yet. Run import_legacy_branches first. Nothing was written."
            )

        rows = legacy.settings(branch_ids)
        if not rows:
            raise CommandError("The export holds no settings for any imported branch.")

        with transaction.atomic():
            created, updated = write(DeviceSettings, rows)
            self.stdout.write(self.style.SUCCESS(
                f"Device settings: {created} created, {updated} updated."
            ))
            self.summarise(rows)

            if options["list"]:
                for pk, defaults, *_ in rows:
                    logo = f"{len(defaults['logo'] or b'') // 1024} KB" if defaults["logo"] else "no logo"
                    self.stdout.write(
                        f"  {pk:>5}  {defaults['settings_code']:<12} "
                        f"{defaults['order_no_prefix']:<6} {defaults['header_1']:<26} "
                        f"feed {defaults['paper_feed']}  {logo}"
                        f"{'' if defaults['is_active'] else '  INACTIVE'}"
                    )

            for line in legacy.skipped(branch_ids):
                self.stdout.write(f"Skipped {line}")

            self.stdout.write(
                "\nShare on WhatsApp is off on every row: it has no legacy column, it "
                "came with the client's feedback on the new screens."
            )

            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("\nDry run -- nothing was written."))

    def summarise(self, rows):
        """The shape of what went in, checkable at a glance."""
        active = [d for _, d, *_ in rows if d["is_active"]]
        logos = [d for _, d, *_ in rows if d["logo"]]
        kb = sum(len(d["logo"]) for d in logos) // 1024
        feeds = {}
        for _, defaults, *_ in rows:
            feeds[defaults["paper_feed"]] = feeds.get(defaults["paper_feed"], 0) + 1

        self.stdout.write(f"  active: {len(active)} of {len(rows)}")
        self.stdout.write(f"  logos: {len(logos)} rows, {kb} KB in total")
        self.stdout.write("  paper feed: " + ", ".join(f"{k} lines x{v}" for k, v in sorted(feeds.items())))
        missing = [code for code in legacy.without_logo()]
        if missing:
            self.stdout.write(f"  no logo in the export: {', '.join(missing)}")
