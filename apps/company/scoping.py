"""Scoping for the Company module.

Company, Branch and Department carry a `company_id`, so they filter directly.

Country, State and Location do not, and should not: the UAE is the UAE. They are
shared reference data, and copying them per company is how a system ends up with
three spellings of the same emirate. So for a company user they are **derived** --
the ones that company actually reaches:

    Company -> its registered country and state
    Branch  -> Location -> State -> Country

A system user sees all of them.
"""

from django.db.models import Q

from apps.company.models import Branch, Company, Country, Department, Location, State
from core.enums import UserScope
from core.scoping import scoped_to


def _is_system(user):
    return user is not None and getattr(user, "scope", UserScope.COMPANY) == UserScope.SYSTEM


def companies_for(user):
    return scoped_to(Company.objects.all(), user, field="id")


def branches_for(user):
    return scoped_to(Branch.objects.all(), user)


def departments_for(user):
    return scoped_to(Department.objects.all(), user)


def countries_for(user):
    """Where this company operates: its registered country, plus every country
    its branches sit in. Derived, so it can never disagree with reality."""
    if _is_system(user):
        return Country.objects.all()
    if user is None or user.company_id is None:
        return Country.objects.none()
    return Country.objects.filter(
        Q(states__locations__branches__company_id=user.company_id)
        | Q(pk__in=Company.objects.filter(pk=user.company_id).values("country_id"))
    ).distinct()


def states_for(user):
    if _is_system(user):
        return State.objects.all()
    if user is None or user.company_id is None:
        return State.objects.none()
    return State.objects.filter(
        Q(locations__branches__company_id=user.company_id)
        | Q(pk__in=Company.objects.filter(pk=user.company_id).values("state_id"))
    ).distinct()


def locations_for(user):
    """Only the zones this company has a branch in.

    A company with no branches yet sees an empty list -- correct, and the Add
    drawer is how the first one gets created.
    """
    if _is_system(user):
        return Location.objects.all()
    if user is None or user.company_id is None:
        return Location.objects.none()
    return Location.objects.filter(branches__company_id=user.company_id).distinct()
