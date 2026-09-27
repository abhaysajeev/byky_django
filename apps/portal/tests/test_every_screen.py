"""Every live web screen in apps/portal/page_registry.py, whatever its app:

- renders, on an empty database, for a role that may read it;
- refuses a role that may not (403), and sends a stranger to sign in;
- is registered as an active, readable web page.

Driven by the registry, so a new screen is covered the moment it is listed --
no per-app copy to forget.
"""

import pytest
from django.urls import reverse

from apps.company.models import Company, Country, State
from apps.portal.models import Page, Role
from apps.portal.page_registry import PAGES, SYSTEM_ONLY
from apps.portal.services import grant_all
from core.enums import Channel, UserScope
from core.models import User

PASSWORD = "Byky#2026"
LIVE = [(code, url_name) for code, _module, _name, url_name, _actions, _order, retired in PAGES
        if url_name and not retired]
RETIRED = [code for code, *_rest, retired in PAGES if retired]
# Signing in always lands somewhere: the dashboard needs no permission.
OPEN_TO_ANY_ROLE = {"general.dashboard"}


@pytest.fixture
def company(db):
    uae = Country.objects.create(short_code="AE", name="United Arab Emirates")
    return Company.objects.create(short_code="BYKY", name="BYKY", country=uae,
                                  state=State.objects.create(country=uae, short_code="AUH", name="Abu Dhabi"),
                                  phone_number="+9710000000", email="ops@byky.test")


def sign_in(client, company, username, *, grant, system=False):
    role = Role.objects.create(company=None if system else company, name=username)
    if grant:
        grant_all(role)
    scope = {"scope": UserScope.SYSTEM} if system else {"company": company}
    User.objects.create_user(username, PASSWORD, display_name=username, role=role,
                             allowed_channels=[Channel.WEB], **scope)
    client.post("/login/", {"username": username, "password": PASSWORD})


def test_every_screen_renders_for_a_role_that_may_read_it(client, company):
    sign_in(client, company, "sara.k", grant=True)
    for code, url_name in LIVE:
        response = client.get(reverse(url_name))
        if code in SYSTEM_ONLY:
            # Every box ticked, still refused: system-only is a scope rule.
            assert response.status_code == 403, code
            continue
        assert response.status_code == 200 and b"byky-sidebar" in response.content, code


def test_system_only_screens_render_for_a_system_user_only(client, company):
    sign_in(client, company, "platform.admin", grant=True, system=True)
    assert SYSTEM_ONLY
    for code, url_name in LIVE:
        if code in SYSTEM_ONLY:
            response = client.get(reverse(url_name))
            assert response.status_code == 200 and b"byky-sidebar" in response.content, code


def test_every_screen_refuses_a_stranger_and_a_role_without_it(client, company):
    for code, url_name in LIVE:
        response = client.get(reverse(url_name))
        assert response.status_code == 302 and response.url.startswith("/login/"), code
    sign_in(client, company, "vis.itor", grant=False)
    for code, url_name in LIVE:
        expected = 200 if code in OPEN_TO_ANY_ROLE else 403
        assert client.get(reverse(url_name)).status_code == expected, code


def test_every_screen_is_registered_for_the_web(db):
    for code, _url_name in LIVE:
        page = Page.objects.get(code=code)
        assert page.is_active and "read" in page.actions and "web" in page.channels, code
        assert page.system_only == (code in SYSTEM_ONLY), code
    assert not Page.objects.filter(code__in=RETIRED, is_active=True).exists()
