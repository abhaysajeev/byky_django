"""Delete request logs and error logs past their keep-for.

The log writer already does this every few hours; this is for doing it now
(after lowering MONITORING_REQUEST_DAYS, say) or from a scheduler.
"""

from django.core.management.base import BaseCommand

from apps.monitoring.services import purge


class Command(BaseCommand):
    help = "Delete request logs and error logs older than their retention period."

    def handle(self, *args, **options):
        deleted = purge(lock=True)
        if not deleted:
            self.stdout.write("Another process is purging right now; nothing done.")
            return
        self.stdout.write(self.style.SUCCESS(
            ", ".join(f"{name}: {count} deleted" for name, count in deleted.items())
        ))
