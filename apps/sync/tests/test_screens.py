"""Logs › Sync Issues on the web: system users only; the list, the detail,
and Replay / Dismiss through their endpoints."""

import json

import pytest

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


def test_a_company_user_is_refused_and_never_sees_the_screen(client, world, parked):
    role = Role.objects.create(company=world["company"], name="Everything")
    grant_all(role)
    User.objects.create_user("boss", PASSWORD, display_name="Boss", company=world["company"], role=role,
                             allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "boss", "password": PASSWORD})

    for url in ("/sync/issues/", f"/sync/issues/{parked['head']}/"):
        assert client.get(url).status_code == 403, url
    response = post_json(client, f"/sync/issues/{parked['head']}/dismiss/", {"reason": "x"})
    assert response.status_code == 403
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
