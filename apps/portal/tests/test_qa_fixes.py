"""QA report 13 Oct 2026, section A -- sign-in and the header:
BUG-001 (a signed-in page is never kept by the browser, so Back after logout
cannot show it) and BUG-008 (Change Password works)."""

import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.company.models import Company, Country, State
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.portal.session_models import AppSession, LogoutReason
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"
CHANGE = "/account/password/"


@pytest.fixture
def sara(db):
    uae = Country.objects.create(short_code="AE", name="United Arab Emirates")
    auh = State.objects.create(country=uae, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(short_code="BYKY", name="BYKY", country=uae, state=auh,
                                     phone_number="+9710000000", email="ops@byky.test")
    role = Role.objects.create(company=company, name="Administrator")
    grant_all(role)
    return User.objects.create_user("sara.k", PASSWORD, display_name="Sara K", company=company, role=role,
                                    allowed_channels=[Channel.WEB])


def signed_in(user, password=PASSWORD):
    browser = Client()
    assert browser.post("/login/", {"username": user.username, "password": password}).status_code == 302
    return browser


def change(browser, **fields):
    return browser.post(CHANGE, json.dumps(fields), content_type="application/json")


# -- BUG-001 ------------------------------------------------------------------------------

def test_a_signed_in_page_is_never_stored(sara):
    browser = signed_in(sara)

    response = browser.get("/dashboard/")

    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]


def test_after_logout_the_page_needs_signing_in_again(sara):
    browser = signed_in(sara)
    browser.get("/logout/")

    response = browser.get("/dashboard/")

    assert response.status_code in (302, 403) or "/login/" in response.get("Location", "")


# -- BUG-008: Change Password -------------------------------------------------------------

def test_the_header_carries_the_working_dialog(sara):
    page = signed_in(sara).get("/dashboard/").content.decode()

    assert "data-cp-modal" in page and 'data-url="/account/password/"' in page
    assert "byky-change-password.js" in page and "css/byky-screen.css" in page
    assert "data-bs-target=\"#byky-change-password\"" not in page


def test_a_password_is_changed_and_other_sessions_signed_out(sara):
    here = signed_in(sara)
    # One web session per person (a second sign-in replaces the first), so the
    # other session is the app the same person is signed in to.
    on_the_app = AppSession.objects.create(user=sara, channel=Channel.MANAGER, logged_in_at=timezone.now())

    response = change(here, current=PASSWORD, new="Fresh#2027", confirm="Fresh#2027")

    assert response.json() == {"ok": True, "message": "Password changed. Your other sessions were signed out."}
    sara.refresh_from_db()
    assert sara.check_password("Fresh#2027")
    assert here.get("/dashboard/").status_code == 200                         # this one stays signed in
    on_the_app.refresh_from_db()
    assert (on_the_app.logged_out_at is not None, on_the_app.logout_reason) == (True, LogoutReason.FORCED)
    signed_in(sara, "Fresh#2027")


@pytest.mark.parametrize("fields, field, message", [
    ({"current": "wrong-one", "new": "Fresh#2027", "confirm": "Fresh#2027"}, "current",
     "The current password is wrong."),
    ({"current": PASSWORD, "new": "short", "confirm": "short"}, "new", "Use at least 8 characters."),
    ({"current": PASSWORD, "new": "Fresh#2027", "confirm": "Fresh#2028"}, "confirm",
     "The two new passwords are not the same."),
    ({"current": PASSWORD, "new": PASSWORD, "confirm": PASSWORD}, "new",
     "Choose a password different from the current one."),
])
def test_each_rule_is_reported_under_its_field(sara, fields, field, message):
    response = change(signed_in(sara), **fields)

    assert response.status_code == 400
    assert response.json()["errors"] == [{"field": field, "message": message}]
    sara.refresh_from_db()
    assert sara.check_password(PASSWORD)


def test_signed_out_it_is_refused(db):
    response = change(Client(), current="x", new="Fresh#2027", confirm="Fresh#2027")

    assert response.status_code == 401
