"""Write the page registry to the database.

Screens used to arrive one data migration per module, which meant the list of
what the software has was spread across the migration history and could only be
read by replaying it. `apps/portal/page_registry.py` holds it instead; this
command applies it, and is safe to run on every deploy.

The work itself is in apps/portal/page_sync.py, because the migration that seeds
a fresh database runs the same registry.

This grants nothing. Access is still `manage.py sync_role_pages` or the
privilege screen (design/rbac.md section 7).
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.portal.page_sync import sync


class Command(BaseCommand):
    help = "Create or update every module and screen listed in apps/portal/page_registry.py."

    @transaction.atomic
    def handle(self, *args, **options):
        created, updated, deactivated = sync()

        self.stdout.write(self.style.SUCCESS(
            f"{created} created, {updated} updated, {len(deactivated)} deactivated."
        ))
        for code in deactivated:
            self.stdout.write(f"  deactivated (not in registry): {code}")
