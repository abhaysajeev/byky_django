"""Buttons follow the role's permissions, not a hardcoded admin flag.

The legacy decided this with `RoleID != 1` in C#, so changing who may create
anything meant a deploy. Here it is a ticked box.
"""

import pytest

from apps.company.models import Company, Country, State
from apps.portal.models import Role, RolePermission
from apps.portal.services import grant_all
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"


@pytest.fixture
def company(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    return Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )


def _sign_in(client, company, name, username):
    role = Role.objects.create(company=company, name=name)
    grant_all(role)
    User.objects.create_user(
        username, PASSWORD, display_name=name, company=company, role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": username, "password": PASSWORD})
    return role


# The label alone is not proof: "Add Branch" is also the drawer's own title,
# and "Export CSV" lives in the shared records modal. Assert the control.
ADD_BRANCH_BUTTON = b'class="scr-btn-primary" data-scr-open="branch:add"'
EXPORT_BUTTON = b"Export\n      </button>"


def test_a_role_with_create_sees_the_add_button(client, company):
    _sign_in(client, company, "Administrator", "admin.one")

    body = client.get("/company/branch/list/").content

    assert ADD_BRANCH_BUTTON in body


def test_a_role_without_create_does_not(client, company):
    role = _sign_in(client, company, "Viewer", "view.one")
    RolePermission.objects.filter(role=role, page__code="company.branch").update(can_create=False)

    body = client.get("/company/branch/list/").content

    assert ADD_BRANCH_BUTTON not in body
    assert b'class="scr-title">Branch' in body      # the screen still renders


def test_a_role_without_print_does_not_see_export(client, company):
    role = _sign_in(client, company, "Viewer", "view.two")
    RolePermission.objects.filter(role=role, page__code="company.branch").update(can_print=False)

    assert EXPORT_BUTTON not in client.get("/company/branch/list/").content


def test_a_role_without_update_does_not_see_edit(client, company):
    role = _sign_in(client, company, "Viewer", "view.three")
    RolePermission.objects.filter(role=role, page__code="company.company").update(can_update=False)

    body = client.get("/company/company/list/").content

    assert b"company:edit" not in body


def test_a_company_administrator_cannot_add_a_company(client, company):
    """Registering a company is platform work, not company work -- it needs the
    permission AND system scope. The system-user side is covered in
    test_scoping.py."""
    _sign_in(client, company, "Administrator", "admin.two")

    assert b'data-scr-open="company:add"' not in client.get("/company/company/list/").content
