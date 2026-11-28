"""Dashboard Privileges (owner, 27 Nov 2026): the dashboard is granted per role
like every other screen, on its own privilege screen just below it; a role
without it lands on the first screen of its sidebar."""

import importlib

import pytest
from django.apps import apps as django_apps
from django.urls import reverse

from apps.portal.models import Page, Role, RolePermission
from apps.portal.tests import test_every_screen as screens
from core.enums import Channel
from core.models import User
from theme.menu import build_menu

PASSWORD = screens.PASSWORD
company = screens.company


def role_with(org, name, *codes):
    """A role holding Read on exactly these pages, with one web user."""
    role = Role.objects.create(company=org, name=name)
    for code in codes:
        RolePermission.objects.create(role=role, page=Page.objects.get(code=code), can_read=True)
    User.objects.create_user(name, PASSWORD, display_name=name, role=role, company=org,
                             allowed_channels=[Channel.WEB])
    return role


def login(client, name):
    client.post("/login/", {"username": name, "password": PASSWORD})


def test_the_dashboard_needs_its_tick(client, company):
    role_with(company, "no.dash", "company.location")
    login(client, "no.dash")
    assert client.get("/dashboard/").status_code == 403
    assert client.get("/dashboard/live/").status_code == 403
    assert client.get("/dashboard/stations/").status_code == 403


def test_with_the_tick_the_dashboard_and_its_data_open(client, company):
    role_with(company, "dash", "general.dashboard")
    login(client, "dash")
    assert client.get("/dashboard/").status_code == 200
    assert client.get("/dashboard/live/").status_code == 200
    assert client.get("/").url == reverse("dashboard")


def test_without_it_home_lands_on_the_first_sidebar_screen(client, company):
    role_with(company, "loc", "company.location", "rental.order")
    login(client, "loc")
    response = client.get("/")
    assert response.status_code == 302 and response.url == reverse("company-location-list")
    menu = build_menu(User.objects.get(username="loc"))["menu"]
    assert not any(item.get("url") == "dashboard" for item in menu)


def test_a_role_with_no_screen_is_told_so(client, company):
    role_with(company, "empty")
    login(client, "empty")
    response = client.get("/")
    assert response.status_code == 200 and b"No screens are assigned to your role yet" in response.content


def test_the_privilege_screen_sits_under_dashboard(client, company):
    screens.sign_in(client, company, "admin", grant=True)
    menu = build_menu(User.objects.get(username="admin"))["menu"]
    assert [item["name"] for item in menu[:2]] == ["Dashboard", "Dashboard Privileges"]
    page = client.get(reverse("dashboard-privileges")).content.decode()
    assert "Privilege Management" in page and "Dashboard" in page


def test_the_privilege_screen_is_refused_without_its_grant(client, company):
    role_with(company, "dash.only", "general.dashboard")
    login(client, "dash.only")
    assert client.get(reverse("dashboard-privileges")).status_code == 403


@pytest.mark.django_db
def test_the_migration_keeps_every_role_on_the_dashboard(company):
    plain = Role.objects.create(company=company, name="Cashier")
    admin = Role.objects.create(company=company, name="Admin")
    RolePermission.objects.create(role=admin, page=Page.objects.get(code="system.privileges"),
                                  can_read=True, can_update=True)
    importlib.import_module("apps.portal.migrations.0044_dashboard_privileges").apply_registry(django_apps, None)

    dash = Page.objects.get(code="general.dashboard")
    assert all(RolePermission.objects.get(role=r, page=dash).can_read for r in (plain, admin))
    screen = Page.objects.get(code="general.privileges")
    assert RolePermission.objects.filter(role=admin, page=screen, can_update=True).exists()
    assert not RolePermission.objects.filter(role=plain, page=screen).exists()
