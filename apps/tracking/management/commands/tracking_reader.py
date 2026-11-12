"""Set up a reader from the command line, until the Readers screen exists
(and for the P0 field test):

    python manage.py tracking_reader list
    python manage.py tracking_reader approve 190619101016097 --company BYKY --branch ZBP1 --name "Zabeel Park 1"
    python manage.py tracking_reader on|off|block 190619101016097
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.company.models import Branch, Company
from apps.tracking.models import Antenna, Reader, ReaderAssignment, ReaderStatus


class Command(BaseCommand):
    help = "List, approve, switch on/off or block an RFID reader."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["list", "approve", "on", "off", "block"])
        parser.add_argument("device_id", nargs="?")
        parser.add_argument("--company", help="company short code (approve)")
        parser.add_argument("--branch", help="station short code (approve)")
        parser.add_argument("--name", default="")

    def handle(self, action, device_id=None, **opts):
        if action == "list":
            for r in Reader.objects.order_by("status", "device_id"):
                at = ReaderAssignment.objects.filter(reader=r, to_at__isnull=True).select_related("branch").first()
                self.stdout.write(f"{r.device_id:16} {r.status:9} {'on ' if r.enabled else 'off'} "
                                  f"{(at.branch.short_code if at else '-'):8} hb={r.last_heartbeat_at} "
                                  f"data={r.last_data_at} ip={r.last_ip} fw={r.firmware}")
            return
        if not device_id:
            raise CommandError("give the reader's device id")
        if action == "approve":
            self._approve(device_id, opts)
            return
        reader = Reader.objects.filter(device_id=device_id).first()
        if not reader:
            raise CommandError(f"no reader {device_id}")
        if action == "block":
            reader.status, reader.enabled = ReaderStatus.BLOCKED, False
        else:
            if reader.status != ReaderStatus.APPROVED:
                raise CommandError("approve it first")
            reader.enabled = action == "on"
        reader.save(update_fields=["status", "enabled", "modified_on"])
        self.stdout.write(f"{device_id}: {reader.status}, {'on' if reader.enabled else 'off'}")

    @transaction.atomic
    def _approve(self, device_id, opts):
        if not opts["company"] or not opts["branch"]:
            raise CommandError("--company and --branch are needed")
        company = Company.objects.filter(short_code__iexact=opts["company"]).first()
        if not company:
            raise CommandError(f"no company {opts['company']}")
        branch = Branch.objects.filter(company=company, short_code__iexact=opts["branch"]).first()
        if not branch:
            raise CommandError(f"no station {opts['branch']} in {company.short_code}")
        reader, _ = Reader.objects.get_or_create(device_id=device_id)
        reader.company, reader.status, reader.enabled = company, ReaderStatus.APPROVED, True
        reader.name = opts["name"] or reader.name or branch.name
        reader.save()
        now = timezone.now()
        open_ = ReaderAssignment.objects.filter(reader=reader, to_at__isnull=True).first()
        if not open_ or open_.branch_id != branch.pk:
            if open_:
                open_.to_at = now
                open_.save(update_fields=["to_at", "modified_on"])
            ReaderAssignment.objects.create(reader=reader, branch=branch, from_at=now)
        for port, direction in zip((1, 2, 3, 4), "ESWN", strict=True):
            Antenna.objects.get_or_create(reader=reader, port=port, defaults={"direction": direction})
        self.stdout.write(f"{device_id}: approved, on, at {branch.name} ({company.short_code})")
