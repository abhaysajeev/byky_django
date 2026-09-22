"""Which rows a user may see.

Permissions answer *what you may do*; scoping answers *which rows you see*.
They are separate questions and separate code.

Two scopes (core.enums.UserScope):

  company  a user of one company, who sees only that company's rows
  system   BYKY's own staff, who see every company

Everything funnels through `scoped_to`. The documented failure of shared-database
multi-tenancy is one query that forgets its filter, so there is exactly one
filter to get right, and it is tested.
"""

from core.enums import UserScope


def scoped_to(queryset, user, field="company"):
    """Limit a queryset to what `user` may see.

    Fails closed: a company user with no company sees nothing. A misconfigured
    account must not fall back to seeing everything -- absence of data should
    never grant power.
    """
    if user is None:
        return queryset.none()
    if getattr(user, "scope", UserScope.COMPANY) == UserScope.SYSTEM:
        return queryset
    if user.company_id is None:
        return queryset.none()
    return queryset.filter(**{field: user.company_id})
