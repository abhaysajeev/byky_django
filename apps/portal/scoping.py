"""Which roles and users a person may see.

Roles and users carry a company, so they filter directly -- with one deliberate
exception: a **system role** (company empty) is BYKY's own, and a company user
never sees, edits or hands out one. That is not the same question as
`scoped_to`'s, which would return every system role to nobody or to everybody
depending on how the null was read, so it is written out here.
"""

from apps.portal.models import Role
from core.models import User
from core.scoping import scoped_to


def roles_for(user):
    """A company user sees their own company's roles; a system user sees all,
    including the system roles they alone may hold."""
    if user is not None and user.sees_every_company:
        return Role.objects.all()
    return scoped_to(Role.objects.filter(company__isnull=False), user)


def users_for(user):
    if user is not None and user.sees_every_company:
        return User.objects.all()
    return scoped_to(User.objects.filter(company__isnull=False), user)
