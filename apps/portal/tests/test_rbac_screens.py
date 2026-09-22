"""The Roles, Users and Privileges screens.

The rules being held in place here are the ones that cannot be recovered from
if they slip: a role that grants something it was never given, a company user
reaching another company's accounts, and an administrator removing their own
access to the only screen that could put it back.
"""

import json

import pytest

from apps.company.models import Company, Country, State
from apps.portal.models import Module, Page, Role, RolePermission
from apps.portal.services import grant_all, has_permission
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


@pytest.fixture
def admin(client, company):
    """A company user whose role may do everything the release ships."""
    role = Role.objects.create(company=company, name="Administrator")
    grant_all(role)
    user = User.objects.create_user(
        "sara.k", PASSWORD, display_name="Sara K", company=company, role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "sara.k", "password": PASSWORD})
    client.user = user
    client.role = role
    return client


@pytest.fixture
def visitor(client, company):
    """Signed in, with a role that grants nothing."""
    role = Role.objects.create(company=company, name="Visitor")
    User.objects.create_user(
        "vis.itor", PASSWORD, display_name="Vis Itor", company=company, role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "vis.itor", "password": PASSWORD})
    return client


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


SCREENS = [
    "/portal/role/list/",
    "/portal/user/list/",
    "/company/privileges/",
    "/crew/privileges/",
]


@pytest.mark.parametrize("url", SCREENS)
def test_every_screen_renders(admin, url):
    assert admin.get(url).status_code == 200


@pytest.mark.parametrize("url", SCREENS)
def test_every_screen_refuses_a_role_without_permission(visitor, url):
    assert visitor.get(url).status_code == 403


# --- Roles -------------------------------------------------------------------

def test_a_new_role_grants_nothing(admin, company):
    response = post_json(admin, "/portal/role/save/", {
        "name": "Station Supervisor", "description": "", "is_active": True,
    })
    assert response.status_code == 200

    role = Role.objects.get(name="Station Supervisor")
    assert role.company_id == company.pk          # never read from the payload
    assert role.permissions.count() == 0


def test_a_role_is_created_into_the_users_own_company(admin, db):
    """A posted company is ignored, not obeyed."""
    other = Company.objects.create(
        short_code="OTHR", name="Other", country=Country.objects.first(),
        state=State.objects.first(), phone_number="+9711111111", email="x@other.test",
    )
    post_json(admin, "/portal/role/save/", {"name": "Snuck In", "company": other.pk})

    assert Role.objects.get(name="Snuck In").company_id != other.pk


def test_a_company_user_never_sees_another_companys_roles(admin, db):
    other = Company.objects.create(
        short_code="OTHR", name="Other", country=Country.objects.first(),
        state=State.objects.first(), phone_number="+9711111111", email="x@other.test",
    )
    Role.objects.create(company=other, name="Their Manager")
    Role.objects.create(name="BYKY Platform Staff")        # a system role

    names = [row["name"] for row in admin.get("/portal/role/list/").context["roles"]]
    assert "Their Manager" not in names
    assert "BYKY Platform Staff" not in names


# --- Users -------------------------------------------------------------------

def test_creating_a_user_hashes_the_password_and_lets_them_sign_in(admin, company, client):
    role = Role.objects.create(company=company, name="Clerk")
    response = post_json(admin, "/portal/user/save/", {
        "username": "Noor.A", "display_name": "Noor A", "role": role.pk,
        "password": PASSWORD, "allowed_channels": ["web"], "is_active": True,
    })
    assert response.status_code == 200, response.content

    user = User.objects.get(display_name="Noor A")
    assert user.username == "noor.a"               # stored lower-case
    assert user.password != PASSWORD
    assert user.check_password(PASSWORD)


def test_a_role_from_another_company_is_refused(admin, db):
    other = Company.objects.create(
        short_code="OTHR", name="Other", country=Country.objects.first(),
        state=State.objects.first(), phone_number="+9711111111", email="x@other.test",
    )
    theirs = Role.objects.create(company=other, name="Their Manager")

    response = post_json(admin, "/portal/user/save/", {
        "username": "mix.up", "display_name": "Mix Up", "role": theirs.pk,
        "password": PASSWORD,
    })
    assert response.status_code == 400
    assert not User.objects.filter(username="mix.up").exists()


def test_a_system_role_cannot_be_given_to_a_company_user(admin, db):
    platform = Role.objects.create(name="BYKY Platform Staff")

    response = post_json(admin, "/portal/user/save/", {
        "username": "climb.er", "display_name": "Climb Er", "role": platform.pk,
        "password": PASSWORD,
    })
    assert response.status_code == 400
    assert not User.objects.filter(username="climb.er").exists()


def test_reset_password_replaces_the_hash_and_closes_sessions(admin, company):
    target = User.objects.create_user(
        "old.hand", PASSWORD, display_name="Old Hand", company=company,
        allowed_channels=[Channel.WEB],
    )
    before = target.password

    response = post_json(
        admin, f"/portal/user/{target.pk}/reset-password/", {"password": "Byky#Fresh1"}
    )
    assert response.status_code == 200

    target.refresh_from_db()
    assert target.password != before
    assert target.check_password("Byky#Fresh1")
    assert target.sessions.filter(logged_out_at__isnull=True).count() == 0


def test_a_short_password_is_refused(admin, company):
    target = User.objects.create_user(
        "old.hand", PASSWORD, display_name="Old Hand", company=company,
    )
    before = target.password

    response = post_json(admin, f"/portal/user/{target.pk}/reset-password/", {"password": "short"})
    assert response.status_code == 400

    target.refresh_from_db()
    assert target.password == before


def test_unlock_clears_the_lockout(admin, company):
    from django.utils import timezone

    target = User.objects.create_user(
        "locked.out", PASSWORD, display_name="Locked Out", company=company,
        failed_login_count=15, locked_until=timezone.now(),
    )
    assert post_json(admin, f"/portal/user/{target.pk}/unlock/", {}).status_code == 200

    target.refresh_from_db()
    assert target.locked_until is None
    assert target.failed_login_count == 0


def test_a_company_user_cannot_touch_another_companys_account(admin, db):
    other = Company.objects.create(
        short_code="OTHR", name="Other", country=Country.objects.first(),
        state=State.objects.first(), phone_number="+9711111111", email="x@other.test",
    )
    theirs = User.objects.create_user(
        "their.staff", PASSWORD, display_name="Their Staff", company=other,
    )

    response = post_json(
        admin, f"/portal/user/{theirs.pk}/reset-password/", {"password": "Byky#Fresh1"}
    )
    assert response.status_code == 404


# --- Privileges --------------------------------------------------------------

@pytest.fixture
def clerk(company):
    return Role.objects.create(company=company, name="Clerk")


def test_adding_a_module_lists_its_screens_with_nothing_ticked(admin, clerk):
    module = Module.objects.get(code="crew")
    response = post_json(admin, "/portal/privileges/add/", {"role": clerk.pk, "module": "crew"})
    assert response.status_code == 200

    rows = RolePermission.objects.filter(role=clerk, page__module=module)
    assert rows.count() == Page.objects.filter(module=module, is_active=True).count()
    assert not any(row.can_read for row in rows)


def test_removing_a_module_leaves_the_roles_other_modules_alone(admin, clerk):
    post_json(admin, "/portal/privileges/add/", {"role": clerk.pk, "module": "crew"})
    post_json(admin, "/portal/privileges/add/", {"role": clerk.pk, "module": "company"})

    post_json(admin, "/portal/privileges/remove/", {"role": clerk.pk, "module": "crew"})

    assert RolePermission.objects.filter(role=clerk, page__module__code="crew").count() == 0
    assert RolePermission.objects.filter(role=clerk, page__module__code="company").count() > 0


def test_saving_the_grid_persists_exactly_what_was_ticked(admin, clerk):
    post_json(admin, "/portal/privileges/add/", {"role": clerk.pk, "module": "crew"})

    response = post_json(admin, "/portal/privileges/save/", {
        "role": clerk.pk, "module": "crew",
        "grants": {"crew.employee": ["read", "update"]},
    })
    assert response.status_code == 200

    row = RolePermission.objects.get(role=clerk, page__code="crew.employee")
    assert (row.can_read, row.can_update) == (True, True)
    assert (row.can_create, row.can_delete, row.can_print) == (False, False, False)
    # Every other screen in the module stays untouched-and-untick=ed.
    assert not RolePermission.objects.get(role=clerk, page__code="crew.designation").can_read


def test_unticking_is_as_real_as_ticking(admin, clerk):
    post_json(admin, "/portal/privileges/add/", {"role": clerk.pk, "module": "crew"})
    post_json(admin, "/portal/privileges/save/", {
        "role": clerk.pk, "module": "crew", "grants": {"crew.employee": ["read"]},
    })

    post_json(admin, "/portal/privileges/save/", {
        "role": clerk.pk, "module": "crew", "grants": {},
    })

    assert not RolePermission.objects.get(role=clerk, page__code="crew.employee").can_read


def test_an_action_a_screen_does_not_offer_cannot_be_granted(admin, clerk):
    """Attendance is read and print: punches arrive from the apps."""
    post_json(admin, "/portal/privileges/add/", {"role": clerk.pk, "module": "crew"})
    post_json(admin, "/portal/privileges/save/", {
        "role": clerk.pk, "module": "crew",
        "grants": {"crew.attendance": ["read", "delete"]},
    })

    row = RolePermission.objects.get(role=clerk, page__code="crew.attendance")
    assert row.can_read is True
    assert row.can_delete is False


def test_a_grid_cannot_be_saved_for_a_role_that_has_not_been_added(admin, clerk):
    response = post_json(admin, "/portal/privileges/save/", {
        "role": clerk.pk, "module": "crew", "grants": {"crew.employee": ["read"]},
    })
    assert response.status_code == 409
    assert RolePermission.objects.filter(role=clerk).count() == 0


def test_a_user_cannot_remove_their_own_access_to_this_screen(admin):
    """Otherwise one save locks everybody out and only a shell can undo it."""
    response = post_json(admin, "/portal/privileges/save/", {
        "role": admin.role.pk, "module": "crew",
        "grants": {"crew.employee": ["read"]},          # crew.privileges dropped
    })
    assert response.status_code == 409
    assert has_permission(admin.user, "crew.privileges", "read") is True


def test_a_user_cannot_remove_their_own_role_from_this_module(admin):
    response = post_json(
        admin, "/portal/privileges/remove/", {"role": admin.role.pk, "module": "crew"}
    )
    assert response.status_code == 409
    assert has_permission(admin.user, "crew.privileges", "read") is True


def test_privilege_writes_refuse_a_role_without_update(visitor, clerk):
    """Posted directly, not by a hidden button."""
    for url in ["/portal/privileges/add/", "/portal/privileges/remove/",
                "/portal/privileges/save/"]:
        response = post_json(visitor, url, {"role": clerk.pk, "module": "crew", "grants": {}})
        assert response.status_code == 403, url
    assert RolePermission.objects.filter(role=clerk).count() == 0


def test_another_companys_role_cannot_be_given_privileges(admin, db):
    other = Company.objects.create(
        short_code="OTHR", name="Other", country=Country.objects.first(),
        state=State.objects.first(), phone_number="+9711111111", email="x@other.test",
    )
    theirs = Role.objects.create(company=other, name="Their Clerk")

    response = post_json(admin, "/portal/privileges/add/", {"role": theirs.pk, "module": "crew"})
    assert response.status_code == 404
    assert RolePermission.objects.filter(role=theirs).count() == 0
