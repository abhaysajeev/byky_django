"""Each copy names its own cookies (config/settings.py COOKIE_PREFIX): dev, qa
and live share one IP, and a browser sends one host's cookies to every port --
under one name, signing in to one copy signed you out of the other."""

import pytest
from django.test import override_settings

from apps.company.models import Company, Country, State
from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"
QA = {"SESSION_COOKIE_NAME": "byky_f_qa_sessionid", "CSRF_COOKIE_NAME": "byky_f_qa_csrftoken"}


@pytest.fixture
def admin(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(short_code="BYKY", name="BYKY", country=country, state=state,
                                     phone_number="+9710000000", email="ops@byky.test")
    role = Role.objects.create(company=company, name="Administrator")
    grant_all(role)
    return User.objects.create_user("sara.k", PASSWORD, display_name="Sara K", company=company, role=role,
                                    allowed_channels=[Channel.WEB])


def sign_in(client):
    client.get("/login/")
    return client.post("/login/", {"username": "sara.k", "password": PASSWORD})


def test_a_copy_signs_in_under_its_own_cookie_names(client, admin):
    with override_settings(**QA):
        sign_in(client)
        assert "byky_f_qa_sessionid" in client.cookies and "sessionid" not in client.cookies
        assert "byky_f_qa_csrftoken" in client.cookies
        page = client.get("/dashboard/")
        assert page.status_code == 200                                    # signed in
        assert 'data-csrf-cookie="byky_f_qa_csrftoken"' in page.content.decode()


def test_another_copys_cookie_no_longer_signs_this_one_out(client, admin):
    with override_settings(**QA):
        sign_in(client)
    # The live copy (another port, same host) sets its own session cookie in
    # the same browser; qa's stays as it was.
    client.cookies["byky_f_live_sessionid"] = "a-live-session-qa-knows-nothing-of"
    with override_settings(**QA):
        response = client.get("/dashboard/")
    assert response.status_code == 200                                    # still signed in to qa
