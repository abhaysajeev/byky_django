"""Grant every registered screen to one role.

Pages ship in migrations; access does not. After a release adds screens, this
is how an existing role is given them -- deliberately a separate, deliberate
step (design/rbac.md section 7).
"""

from django.core.management.base import BaseCommand, CommandError

from apps.portal.models import Role
from apps.portal.services import grant_all


class Command(BaseCommand):
    help = "Tick every action of every active page for the named role."

    def add_arguments(self, parser):
        parser.add_argument("--role", required=True)
        parser.add_argument("--company", help="Company short code, if the role name is not unique")

    def handle(self, *args, **options):
        roles = Role.objects.filter(name=options["role"])
        if options["company"]:
            roles = roles.filter(company__short_code=options["company"])
        if not roles.exists():
            raise CommandError(f"No role named {options['role']!r}.")
        if roles.count() > 1:
            raise CommandError("That role name exists in several companies; pass --company.")

        role = roles.first()
        granted = grant_all(role)
        # A system role belongs to no company (design/rbac.md section 7).
        scope = role.company.name if role.company_id else "every company"
        self.stdout.write(self.style.SUCCESS(
            f"{role.name} ({scope}): {granted} screens granted."
        ))
