"""Every drawer dropdown has something behind it.

`theme.drawers.resolve` treats an `options_from` key the context does not carry
as an empty list, on purpose: a 500 on an unrelated screen is worse than an
empty select. The cost is that the mistake is invisible -- and it happened
twice. The crew screens never supplied `companies_list`, so a system user's
Company dropdown on Add Employee was empty and the save could not be completed
by anyone; the Branch drawer's three approval pickers read `employees`, which
the view hardcoded to `[]` long after the crew module existed.

Both were only visible to a system user, because a company user never sees a
company field at all. So the check is made here instead, against the context
each screen actually renders.
"""

import pytest

from apps.company.models import (
    Branch,
    Company,
    Country,
    Department,
    Location,
    State,
)
from apps.crew.models import Designation, Employee
from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import Channel, UserScope
from core.models import User

PASSWORD = "Byky#2026"

# url -> the drawer context variables that screen renders
SCREENS = {
    "/company/branch/list/": ["drawer_branch"],
    "/company/department/list/": ["drawer_department"],
    "/company/location/list/": ["drawer_location"],
    "/company/country-state/list/": ["drawer_country", "drawer_state"],
    "/crew/employee/list/": ["drawer_employee"],
    "/crew/designation/list/": ["drawer_designation"],
    "/crew/address/list/": ["drawer_address"],
    "/portal/role/list/": ["drawer_role"],
    "/portal/user/list/": ["drawer_user"],
}


@pytest.fixture
def world(db):
    """One row of everything a drawer can offer.

    Deliberately complete: the assertion below is that no dropdown resolves to
    an empty list, and it can only mean "the wiring is wrong" if the data it
    would show exists.
    """
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )
    location = Location.objects.create(
        country=country, state=state, short_code="CORN", name="Corniche"
    )
    Department.objects.create(company=company, short_code="OPS", name="Operations")
    Branch.objects.create(
        company=company, location=location, short_code="C1", name="Corniche 1"
    )
    designation = Designation.objects.create(
        company=company, code="MECH", title="Mechanic", rank_order=1
    )
    Employee.objects.create(
        company=company, employee_code="BYKY001", first_name="Anil", last_name="R",
        designation=designation,
    )
    return company


@pytest.fixture
def system_user(client, world):
    """BYKY's own staff: the only scope that sees the company field at all."""
    role = Role.objects.create(name="BYKY Platform Staff")
    grant_all(role)
    User.objects.create_user(
        "platform.staff", PASSWORD, display_name="Platform Staff",
        scope=UserScope.SYSTEM, company=None, role=role, allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "platform.staff", "password": PASSWORD})
    return client


@pytest.mark.parametrize("url,specs", SCREENS.items())
def test_every_dropdown_on_every_screen_has_options(system_user, url, specs):
    context = system_user.get(url).context

    for spec_name in specs:
        spec = context[spec_name]
        for section in spec["sections"]:
            for field in section["fields"]:
                source = field.get("options_from")
                if not source:
                    continue
                assert source in context, (
                    f"{url}: {spec_name}.{field['id']} reads '{source}', "
                    f"which that screen's view does not supply"
                )
                assert field.get("resolved"), (
                    f"{url}: {spec_name}.{field['id']} resolved to an empty list "
                    f"from '{source}'"
                )
