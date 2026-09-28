"""Delete a company's vehicles that stand at no branch, then renumber the
Vehicle Identifier of every vehicle left, gap-free from VB0001.

Why this exists: the first import (import_live_vehicle_data) brought in the
whole legacy stock register -- the old system never deleted a vehicle, only
switched it off -- so it holds old, retired and unaccounted-for vehicles. The
client's sheet (import_vehicle_tags) placed the fleet actually on the ground
at a branch; decided with the user, everything else is removed.

Nothing references a vehicle yet (no rentals), so the delete is clean; the
command refuses if that is no longer true rather than cascading. Dry run
unless --commit: the report shows what would change, and nothing is written.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import ProtectedError

from apps.company.models import Company
from apps.fleet.models import IDENTIFIER_DIGITS, IDENTIFIER_PREFIX, Vehicle
from apps.fleet.services import renumber_identifiers


def identifier(number):
    """The text the database makes of an identifier number (Vehicle.identifier)."""
    return f"{IDENTIFIER_PREFIX}{number:0{IDENTIFIER_DIGITS}d}"


class Command(BaseCommand):
    help = "Delete a company's vehicles with no branch and renumber every identifier from VB0001."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Company short_code whose unplaced vehicles go")
        parser.add_argument("--commit", action="store_true", help="Write the changes (default: dry run)")

    def handle(self, *args, **options):
        company = Company.objects.filter(short_code=options["company"]).first()
        if company is None:
            raise CommandError(f"No company with short_code={options['company']!r}.")

        unplaced = Vehicle.objects.filter(company=company, current_branch__isnull=True)
        active = unplaced.filter(is_active=True).count()
        doomed = unplaced.count()
        placed = Vehicle.objects.filter(company=company, current_branch__isnull=False).count()
        if options["commit"]:
            with transaction.atomic():
                try:
                    unplaced.delete()
                except ProtectedError as error:
                    raise CommandError(f"{len(error.protected_objects)} record(s) still refer to these "
                                       "vehicles; nothing was deleted.") from error
                numbered = renumber_identifiers()
        else:
            # No trial run inside a rolled-back transaction: a sequence reset is
            # never rolled back in Postgres, so a dry run only counts.
            numbered = Vehicle.objects.count() - doomed
        first, last = (identifier(1), identifier(numbered)) if numbered else ("-", "-")

        done = options["commit"]
        self.stdout.write(
            f"{company.name}: {doomed} vehicle(s) with no branch {'deleted' if done else 'to delete'} "
            f"({active} marked active, {doomed - active} inactive); {placed} at a branch kept.\n"
            f"Identifiers {'renumbered' if done else 'to renumber'}: {numbered} vehicle(s), {first} to {last}."
        )
        self.stdout.write(self.style.SUCCESS("Committed.") if options["commit"]
                          else self.style.WARNING("Dry run: nothing written. Re-run with --commit."))
