"""The permission rules from design/rbac.md, held in place by tests.

These are the checks the legacy never had: it hid buttons and left the save
actions unguarded.
"""

import pytest
from django.db import IntegrityError

from apps.company.models import Company, Country, State
from apps.portal.models import Module, Page, Role, RolePermission
from apps.portal.services import (
    add_role_to_module,
    has_permission,
    permissions_for,
    remove_role_from_module,
)
from core.models import User


@pytest.fixture
def world(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )
    fleet = Module.objects.create(code="fleet", name="Vehicle Management", sort_order=1)
    rental = Module.objects.create(code="rental", name="Rental Management", sort_order=2)
    vehicle = Page.objects.create(
        module=fleet, code="fleet.vehicle", name="Vehicles", url_name="fleet-vehicle-list",
        channels=["web", "operator"], actions=["create", "read", "update", "delete"],
    )
    order = Page.objects.create(
        module=rental, code="rental.order", name="Rentals", url_name="rental-order-list",
        channels=["web"], actions=["read", "print"],
    )
    role = Role.objects.create(company=company, name="Branch Manager")
    user = User.objects.create_user(
        "sara.k", "pw", display_name="Sara K", company=company, role=role,
        allowed_channels=["web"],
    )
    return {
        "company": company, "fleet": fleet, "rental": rental,
        "vehicle": vehicle, "order": order, "role": role, "user": user,
    }


def test_a_role_with_no_permissions_grants_nothing(world):
    assert has_permission(world["user"], "fleet.vehicle", "read") is False


def test_adding_a_module_lists_its_screens_but_grants_nothing(world):
    add_role_to_module(world["role"], world["fleet"])

    assert RolePermission.objects.filter(role=world["role"]).count() == 1
    assert has_permission(world["user"], "fleet.vehicle", "read") is False


def test_ticking_a_box_grants_exactly_that_action(world):
    add_role_to_module(world["role"], world["fleet"])
    RolePermission.objects.filter(role=world["role"]).update(can_read=True)

    assert has_permission(world["user"], "fleet.vehicle", "read") is True
    assert has_permission(world["user"], "fleet.vehicle", "update") is False


def test_an_action_the_screen_does_not_offer_is_never_granted(world):
    """The rentals screen offers read and print only. A stray can_approve on the
    row must not grant approve."""
    permission = RolePermission.objects.create(
        role=world["role"], page=world["order"], can_read=True, can_approve=True
    )
    assert permission.can_approve is True
    assert has_permission(world["user"], "rental.order", "approve") is False


def test_a_user_without_a_role_has_no_permissions(world):
    world["user"].role = None
    world["user"].save()

    assert has_permission(world["user"], "fleet.vehicle", "read") is False


def test_an_inactive_user_has_no_permissions(world):
    add_role_to_module(world["role"], world["fleet"])
    RolePermission.objects.filter(role=world["role"]).update(can_read=True)
    world["user"].is_active = False

    assert has_permission(world["user"], "fleet.vehicle", "read") is False


def test_an_inactive_page_grants_nothing(world):
    add_role_to_module(world["role"], world["fleet"])
    RolePermission.objects.filter(role=world["role"]).update(can_read=True)
    Page.objects.filter(pk=world["vehicle"].pk).update(is_active=False)

    assert has_permission(world["user"], "fleet.vehicle", "read") is False


def test_removing_a_module_leaves_the_other_modules_alone(world):
    add_role_to_module(world["role"], world["fleet"])
    add_role_to_module(world["role"], world["rental"])

    remove_role_from_module(world["role"], world["fleet"])

    remaining = RolePermission.objects.filter(role=world["role"])
    assert remaining.count() == 1
    assert remaining.first().page.code == "rental.order"


def test_permissions_for_a_channel_lists_only_that_apps_screens(world):
    add_role_to_module(world["role"], world["fleet"])
    add_role_to_module(world["role"], world["rental"])
    RolePermission.objects.update(can_read=True)

    web = [row["page"] for row in permissions_for(world["user"], "web")]
    operator = [row["page"] for row in permissions_for(world["user"], "operator")]

    assert web == ["fleet.vehicle", "rental.order"]
    assert operator == ["fleet.vehicle"]


def test_permission_rows_carry_only_the_screens_own_actions(world):
    RolePermission.objects.create(role=world["role"], page=world["order"], can_read=True)

    row = permissions_for(world["user"], "web")[0]

    assert row["read"] is True
    assert "print" in row
    assert "approve" not in row


def test_one_role_name_per_company(world):
    with pytest.raises(IntegrityError):
        Role.objects.create(company=world["company"], name="Branch Manager")
