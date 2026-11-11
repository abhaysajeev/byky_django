"""Fill the dial-code table (apps/company/dial_codes.py).

    python manage.py load_dial_codes             # from the bundled file
    python manage.py load_dial_codes --rebuild   # first rebuild the file from
                                                 # libphonenumber + CLDR (dev only)

Re-runnable: a person's is_active and sort_order are kept.
"""

from django.core.management.base import BaseCommand

from apps.company import dial_codes
from apps.company.models import DialCode


class Command(BaseCommand):
    help = "Load the dial codes for mobile numbers."

    def add_arguments(self, parser):
        parser.add_argument("--rebuild", action="store_true",
                            help="rebuild the bundled file from the phonenumbers and babel packages first")

    def handle(self, *args, rebuild=False, **options):
        if rebuild:
            rows = dial_codes.build()
            dial_codes.write_file(rows)
            self.stdout.write(f"Wrote {len(rows)} countries to {dial_codes.DATA_FILE}.")
        created, updated = dial_codes.load(DialCode, dial_codes.read_file())
        self.stdout.write(f"Dial codes: {created} added, {updated} updated.")
