"""Delete uploaded photos no request attached within PHOTO_UNATTACHED_HOURS.

Beat runs the same thing every night (apps.photos.tasks.purge_unattached);
this is for running it now.
"""

from django.core.management.base import BaseCommand

from apps.photos.services import purge_unattached


class Command(BaseCommand):
    help = "Delete uploaded photos that no request attached in time."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(f"{purge_unattached()} unattached photos deleted."))
