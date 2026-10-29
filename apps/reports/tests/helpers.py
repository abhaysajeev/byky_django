"""Shared by the report tests: a signed-in web user whose role has every
screen, and the report JSON on 2 Oct 2026 -- the day the rental test bookings
are made."""

import pytest

from apps.portal.models import Role
from apps.portal.services import grant_all
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"
DAY = {"from": "2026-10-02T00:00", "to": "2026-10-02T23:59"}


@pytest.fixture
def web(client, world):
    role = Role.objects.create(company=world["company"], name="Reports")
    grant_all(role)
    User.objects.create_user("rep.user", PASSWORD, display_name="Rana", company=world["company"], role=role,
                             allowed_channels=[Channel.WEB])
    assert client.post("/login/", {"username": "rep.user", "password": PASSWORD}).status_code in (200, 302)
    client.role = role
    return client


def data(client, slug, **params):
    response = client.get(f"/reports/{slug}/data/", {**DAY, **params})
    assert response.status_code == 200, response.content[:500]
    return response.json()


def page_rows(d):
    return [r for r in d["rows"] if r["kind"] == "data"]
