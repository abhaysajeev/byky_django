"""Create the first company, role and web user.

There is no Django admin in this project, so this command is how a fresh
database gets its first sign-in.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.company.models import Company, Country, State
from apps.portal.models import Module, Page, Role
from apps.portal.services import grant_all
from core.enums import ApprovalStatus, Channel
from core.models import User


class Command(BaseCommand):
    help = "Create a company, an Administrator role and a web user."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--name", default="Administrator")
        parser.add_argument("--company", default="BYKY")
        parser.add_argument("--company-code", default="BYKY")
        parser.add_argument("--timezone", default="Asia/Dubai")

    @transaction.atomic
    def handle(self, *args, **options):
        if User.objects.filter(username=User.normalize_username(options["username"])).exists():
            raise CommandError("That username already exists.")

        country, _ = Country.objects.get_or_create(
            short_code="AE", defaults={"name": "United Arab Emirates"}
        )
        state, _ = State.objects.get_or_create(
            country=country, short_code="AUH", defaults={"name": "Abu Dhabi"}
        )
        company, _ = Company.objects.get_or_create(
            short_code=options["company_code"],
            defaults={
                "name": options["company"],
                "timezone": options["timezone"],
                "country": country,
                "state": state,
                "phone_number": "",
                "email": "",
                "approval_status": ApprovalStatus.APPROVED,
            },
        )
        role, _ = Role.objects.get_or_create(
            company=company, name="Administrator",
            defaults={"description": "Full access", "approval_status": ApprovalStatus.APPROVED},
        )

        # Every screen registered so far, fully ticked for this role.
        pages = Page.objects.filter(is_active=True)
        grant_all(role)

        user = User.objects.create_user(
            options["username"], options["password"],
            display_name=options["name"],
            company=company,
            role=role,
            allowed_channels=[Channel.WEB],
            approval_status=ApprovalStatus.APPROVED,
        )

        self.stdout.write(self.style.SUCCESS(
            f"Created {user.username} for {company.name} "
            f"({Module.objects.count()} modules, {pages.count()} screens granted)."
        ))
