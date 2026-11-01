"""Logs › Sync Issues on the web: the list, the detail, and Replay / Dismiss
through their endpoints. A company user with the Read tick sees their own
company's issues; Replay and Dismiss are system admins' only (1 Nov 2026)."""

import json

import pytest

from apps.company.models import Company
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.rental.models import OrderItem, OrderItemStatus
from apps.sync.models import IssueStatus, SyncIssue
from apps.sync.tests import test_parking as parking_tests
from core.enums import Channel
from core.models import User

send, back = parking_tests.send, parking_tests.back
world, token, fresh_throttle, shop = parking_tests.world, parking_tests.token, parking_tests.fresh_throttle, \
    parking_tests.shop
booked, office = parking_tests.booked, parking_tests.office
RETURN, PASSWORD = parking_tests.RETURN, parking_tests.orders.fare_api.PASSWORD


@pytest.fixture
def parked(client, token, booked):
    """A wrong return parked, and the other vehicle's return waiting behind it."""
    wrong = back(booked, total_amount="55.00")
    head = send(client, RETURN, wrong, token=token, park=True).json()["data"]
    follower = back(booked, 1)
    send(client, RETURN, follower, token=token)
    return {"wrong": wrong, "head": head["issue_id"],
            "follower": SyncIssue.objects.get(sync_id=follower["sync_id"]).pk}


@pytest.fixture
def signed_in(client, office):
    client.post("/login/", {"username": "platform.admin", "password": PASSWORD})
    return client


def post_json(client, url, payload):
    return client.post(url, json.dumps(payload), content_type="application/json")


@pytest.fixture
def company_admin(client, world):
    """The company's Administrator role: every screen ticked, as on live."""
    role = Role.objects.create(company=world["company"], name="Administrator")
    grant_all(role)
    User.objects.create_user("boss", PASSWORD, display_name="Boss", company=world["company"], role=role,
                             allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "boss", "password": PASSWORD})
    return client


def test_a_company_admin_sees_their_companys_issues(company_admin, parked):
    page = company_admin.get("/sync/issues/").content.decode()

    assert f"#{parked['head']}" in page and "amount_mismatch" in page
    assert "Sync Issues" in company_admin.get("/dashboard/").content.decode()


def test_a_company_admin_views_but_cannot_replay_or_dismiss(company_admin, parked):
    head = parked["head"]
    page = company_admin.get(f"/sync/issues/{head}/").content.decode()

    assert "amount_mismatch" in page
    assert "data-sync-replay" not in page and "data-sync-dismiss" not in page and "data-sync-body" not in page
    assert "/monitoring/requests/" not in page and "/monitoring/errors/" not in page
    for action, payload in (("replay", {}), ("dismiss", {"reason": "x"})):
        response = post_json(company_admin, f"/sync/issues/{head}/{action}/", payload)
        assert response.status_code == 403, action
    assert SyncIssue.objects.get(pk=head).status == IssueStatus.OPEN


def test_a_company_admin_never_sees_another_companys_issue(company_admin, world, parked):
    mine = world["company"]
    other = Company.objects.create(short_code="ZSYNC", name="Other Rentals", country=mine.country, state=mine.state,
                                   phone_number="+9711111111", email="ops@other.test")
    SyncIssue.objects.update(company=other)            # the head and the call waiting on it

    for pk in (parked["head"], parked["follower"]):
        assert company_admin.get(f"/sync/issues/{pk}/").status_code == 404, pk
    assert "No issues match" in company_admin.get("/sync/issues/?status=all").content.decode()


def test_the_other_logs_stay_system_only_for_a_company_admin(company_admin):
    for url in ("/monitoring/requests/", "/monitoring/errors/"):
        assert company_admin.get(url).status_code == 403, url


def test_a_company_user_without_the_tick_is_refused(client, world, parked):
    role = Role.objects.create(company=world["company"], name="Cashier")
    User.objects.create_user("cashier", PASSWORD, display_name="Cashier", company=world["company"], role=role,
                             allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "cashier", "password": PASSWORD})

    assert client.get("/sync/issues/").status_code == 403
    assert "Sync Issues" not in client.get("/dashboard/").content.decode()


def test_the_list_shows_open_issues_and_who_waits_on_whom(signed_in, parked):
    page = signed_in.get("/sync/issues/").content.decode()

    assert f"#{parked['head']}" in page and f"Waiting on #{parked['head']}" in page
    assert "amount_mismatch" in page
    assert "Sync Issues" in signed_in.get("/dashboard/").content.decode()


def test_the_list_filters(signed_in, parked):
    assert "No issues match" in signed_in.get("/sync/issues/?status=resolved").content.decode()
    assert f"#{parked['head']}" in signed_in.get(f"/sync/issues/?q={parked['head']}").content.decode()
    by_sync_id = signed_in.get(f"/sync/issues/?q={parked['wrong']['sync_id']}").content.decode()
    assert f"#{parked['head']}" in by_sync_id and f"#{parked['follower']}" not in by_sync_id
    assert signed_in.get("/sync/issues/?q=not-a-uuid&status=all").status_code == 200


def test_the_detail_offers_replay_only_on_the_head(signed_in, parked):
    head = signed_in.get(f"/sync/issues/{parked['head']}/").content.decode()
    follower = signed_in.get(f"/sync/issues/{parked['follower']}/").content.decode()

    assert "data-sync-replay" in head and "data-sync-body" in head
    assert "data-sync-replay" not in follower and f"issue {parked['head']}" in follower
    assert signed_in.get("/sync/issues/999999/").status_code == 404


def test_replay_from_the_screen_records_the_head_and_its_follower(signed_in, parked):
    fixed = {**parked["wrong"], "total_amount": "50.00"}

    body = post_json(signed_in, f"/sync/issues/{parked['head']}/replay/", {"request_data": fixed}).json()

    assert body["ok"] is True, body
    assert body["message"] == f"Recorded: issues {parked['head']}, {parked['follower']}."
    assert set(SyncIssue.objects.values_list("status", flat=True)) == {IssueStatus.RESOLVED}
    assert set(OrderItem.objects.values_list("status", flat=True)) == {OrderItemStatus.RETURNED}


def test_replay_from_the_screen_reports_a_refusal_and_bad_json(signed_in, parked):
    still_wrong = post_json(signed_in, f"/sync/issues/{parked['head']}/replay/", {}).json()
    bad_json = post_json(signed_in, f"/sync/issues/{parked['head']}/replay/", {"request_data": "{nope"}).json()
    not_head = post_json(signed_in, f"/sync/issues/{parked['follower']}/replay/", {}).json()

    assert still_wrong["ok"] is True and still_wrong["message"].startswith(f"Issue {parked['head']} was refused")
    assert bad_json["ok"] is False and "not valid JSON" in bad_json["errors"][0]["message"]
    assert not_head["ok"] is False and "waits behind" in not_head["errors"][0]["message"]


def test_dismiss_from_the_screen(signed_in, parked):
    refused = post_json(signed_in, f"/sync/issues/{parked['head']}/dismiss/", {"reason": ""}).json()
    body = post_json(signed_in, f"/sync/issues/{parked['head']}/dismiss/", {"reason": "Paper return"}).json()

    assert refused["ok"] is False
    assert body == {"ok": True, "message": f"Dismissed: issues {parked['head']}, {parked['follower']}."}
    page = signed_in.get(f"/sync/issues/{parked['follower']}/").content.decode()
    assert "Paper return" in page and "data-sync-replay" not in page
