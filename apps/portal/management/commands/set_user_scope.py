"""Promote a user to system scope, or return them to one company.

System scope is cross-company visibility, and it is granted deliberately --
never by leaving a field blank. Who holds it is one query:

    SELECT username FROM "user" WHERE scope = 'system';
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.company.models import Company
from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import UserScope
from core.models import User


class Command(BaseCommand):
    help = "Set a user's scope to 'system' (sees every company) or 'company'."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--scope", choices=[UserScope.SYSTEM, UserScope.COMPANY], required=True)
        parser.add_argument("--company", help="Company short code. Required when moving to company scope.")
        parser.add_argument(
            "--role",
            default="System Administrator",
            help="Role to give a system user. Created, with every screen granted, if absent.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        username = User.normalize_username(options["username"])
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"No user named {username!r}.")

        if options["scope"] == UserScope.SYSTEM:
            # A system user holds a system role: one that belongs to no company.
            # Permissions are still checked -- scope decides which rows are
            # visible, never which actions are allowed (design/rbac.md 7).
            role, created = Role.objects.get_or_create(
                company=None, name=options["role"],
                defaults={"description": "Cross-company access for platform staff"},
            )
            if created:
                grant_all(role)
            user.scope = UserScope.SYSTEM
            user.company = None
            user.role = role
            user.save()
            self.stdout.write(self.style.SUCCESS(
                f"{user.username} now sees every company, as {role.name}."
            ))
            return

        if not options["company"]:
            raise CommandError("Moving to company scope needs --company <short code>.")
        company = Company.objects.filter(short_code=options["company"]).first()
        if company is None:
            raise CommandError(f"No company with code {options['company']!r}.")

        user.scope = UserScope.COMPANY
        user.company = company
        if user.role and user.role.company_id is None:
            user.role = None        # a system role cannot be held inside a company
        user.save()
        self.stdout.write(self.style.SUCCESS(
            f"{user.username} is now scoped to {company.name}."
            + ("" if user.role else " Assign them a role in that company.")
        ))
