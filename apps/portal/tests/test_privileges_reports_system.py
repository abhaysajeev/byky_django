"""Reports and Users & Roles have their own Privileges screens (owner, 3 Nov
2026): company admins use custom roles, and these two modules could not be
granted from the web."""

import importlib

import pytest
from django.apps import apps as django_apps
from django.test import Client

from apps.portal.models import Page, Role, RolePermission
from apps.portal.tests import test_rbac_screens as rbac
from core.enums import Channel
from core.models import User

PASSWORD, post_json = rbac.PASSWORD, rbac.post_json
company, admin, visitor, clerk = rbac.company, rbac.admin, rbac.visitor, rbac.clerk

REPORTS_PRIV = "/reports/privileges/"
SYSTEM_PRIV = "/portal/privileges/system/"


@pytest.mark.parametrize("url", [REPORTS_PRIV, SYSTEM_PRIV])
def test_the_screens_open_for_a_role_that_has_them(admin, url):
    assert admin.get(url).status_code == 200


@pytest.mark.parametrize("url", [REPORTS_PRIV, SYSTEM_PRIV])
def test_the_screens_are_refused_without_the_page(visitor, url):
    assert visitor.get(url).status_code == 403


def test_the_reports_grid_lists_every_report(admin):
    body = admin.get(REPORTS_PRIV).content.decode()
    for name in ("Rent Summary", "Consolidated", "Rent Details", "Asset Details"):
        assert name in body


def test_the_users_and_roles_grid_lists_roles_and_users(admin):
    body = admin.get(SYSTEM_PRIV).content.decode()
    assert "Roles" in body and "Users" in body


def _sign_in_as(role, username):
    """A fresh browser signed in as a new user of `role`."""
    User.objects.create_user(username, PASSWORD, display_name=username, company=role.company, role=role,
                             allowed_channels=[Channel.WEB])
    browser = Client()
    browser.post("/login/", {"username": username, "password": PASSWORD})
    return browser


def test_a_report_granted_here_opens_for_a_custom_role(admin, clerk):
    post_json(admin, "/portal/privileges/add/", {"role": clerk.pk, "module": "reports"})
    response = post_json(admin, "/portal/privileges/save/", {
        "role": clerk.pk, "module": "reports", "grants": {"reports.rent_summary": ["read"]}})
    assert response.status_code == 200

    reader = _sign_in_as(clerk, "report.reader")
    assert reader.get("/reports/rent-summary/").status_code == 200
    assert reader.get("/reports/consolidated/").status_code == 403


def test_user_management_granted_here_opens_for_a_custom_role(admin, clerk):
    post_json(admin, "/portal/privileges/add/", {"role": clerk.pk, "module": "system"})
    post_json(admin, "/portal/privileges/save/", {
        "role": clerk.pk, "module": "system", "grants": {"system.user": ["read"]}})

    reader = _sign_in_as(clerk, "user.reader")
    assert reader.get("/portal/user/list/").status_code == 200
    assert reader.get("/portal/role/list/").status_code == 403


def test_saving_needs_this_modules_privileges_update(admin, clerk):
    RolePermission.objects.filter(role=admin.role, page__code="reports.privileges").update(can_update=False)
    response = post_json(admin, "/portal/privileges/save/", {
        "role": clerk.pk, "module": "reports", "grants": {"reports.rent_summary": ["read"]}})
    assert response.status_code == 403


def test_the_migration_grants_both_screens_to_roles_that_manage_privileges(company):
    manager = Role.objects.create(company=company, name="Custom admin")
    plain = Role.objects.create(company=company, name="Cashier")
    RolePermission.objects.create(role=manager, page=Page.objects.get(code="company.privileges"),
                                  can_read=True, can_update=True)
    RolePermission.objects.create(role=plain, page=Page.objects.get(code="company.branch"), can_read=True)

    migration = importlib.import_module("apps.portal.migrations.0032_reports_system_privileges")
    migration.apply_registry(django_apps, None)

    for code in ("reports.privileges", "system.privileges"):
        row = RolePermission.objects.get(role=manager, page__code=code)
        assert (row.can_read, row.can_update) == (True, True)
        assert not RolePermission.objects.filter(role=plain, page__code=code).exists()
