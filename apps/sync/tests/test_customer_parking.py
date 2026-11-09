"""A parked customer create, and the bookings that wait for it (owner,
7 Nov 2026): customer create parks like every recording call except
`customer_exists`; a booking naming the customer by `customer_sync_id` waits
behind its parked create and runs when the office replays it."""

import pytest

from apps.rental import services as rental_services
from apps.rental.models import Customer, Order, OrderItem, OrderItemStatus
from apps.rental.tests.conftest import make_customer
from apps.sync import services
from apps.sync.models import IssueKind, IssueReason, IssueStatus, SyncIssue
from apps.sync.tests import test_parking as parking_calls
from apps.sync.tests.test_parking import BOOK, RETURN, STATUS, back, booking, every_issue, send
from apps.sync.views import _annotate
from core.ids import uuid7

world, token, fresh_throttle, shop = parking_calls.world, parking_calls.token, parking_calls.fresh_throttle, parking_calls.shop
office = parking_calls.office

CREATE = "/api/v1/operator/customers/create"


def new_customer(**overrides):
    data = {"sync_id": str(uuid7()), "first_name": "Mariam", "last_name": "Saeed",
            "mobile_country_code": "+971", "mobile_no": "509876543", "full_number": "971509876543"}
    data.update(overrides)
    return data


def wrong_phone(**overrides):
    """Refused invalid_request: full_number does not match the other two."""
    return new_customer(full_number="971500000000", **overrides)


def booking_for(world, shop, customer, number=231):
    data = booking(world, shop, number=number)
    data.pop("customer_id")
    data["customer_sync_id"] = customer["sync_id"]
    return data


# -- Customer create parks ---------------------------------------------------------------

def test_a_refused_customer_create_is_parked_then_replayed(client, token, office):
    wrong = wrong_phone()

    body = send(client, CREATE, wrong, token=token, park=True).json()

    assert body["code"] == "parked", body
    issue = SyncIssue.objects.get()
    assert (issue.kind, issue.reason, issue.error_code, issue.order_id) == (
        IssueKind.CUSTOMER_CREATE, IssueReason.REFUSED, "invalid_request", None)
    assert body["data"]["order_id"] is None and body["data"]["waiting_on"] is None
    assert not Customer.objects.filter(sync_id=wrong["sync_id"]).exists()
    # A resend while open is answered parked again, and not run.
    assert send(client, CREATE, wrong, token=token).json()["code"] == "parked"
    assert issue.attempts.count() == 1

    fixed = {**wrong, "full_number": "971509876543"}
    outcome = services.replay(every_issue(), issue.pk, user=office, request_data=fixed)

    assert outcome == {"resolved": [issue.pk], "stopped_at": None}
    customer = Customer.objects.get(sync_id=wrong["sync_id"])
    assert customer.first_name == "Mariam"
    issue.refresh_from_db()
    assert (issue.status, issue.reply["code"], issue.reply["data"]["customer_id"]) == (
        IssueStatus.RESOLVED, "ok", customer.pk)
    assert send(client, CREATE, fixed, token=token).json()["code"] == "duplicate"


def test_without_park_a_refused_customer_create_is_answered_as_before(client, token):
    response = send(client, CREATE, wrong_phone(), token=token)

    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")
    assert not every_issue().exists()


def test_a_crashed_customer_create_is_parked_as_server_error(client, token, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(rental_services, "create_customer", crash)

    assert send(client, CREATE, new_customer(), token=token, park=True).json()["code"] == "parked"
    assert (SyncIssue.objects.get().reason, SyncIssue.objects.get().error_code) == (
        IssueReason.SERVER_ERROR, "server_error")


@pytest.mark.parametrize("own_company", [True, False], ids=["this-company", "another-company"])
def test_customer_exists_is_never_parked(client, world, token, own_company):
    company = world["company"] if own_company else world["other"]
    make_customer(world, company=company, customer_code="CU090", mobile_no="509876543")

    response = send(client, CREATE, new_customer(), token=token, park=True)

    assert (response.status_code, response.json()["code"]) == (409, "customer_exists")
    assert bool(response.json()["data"]) is own_company
    assert not every_issue().exists()


# -- A booking that names its customer by customer_sync_id -------------------------------

def test_a_booking_waits_behind_its_parked_customer_then_runs_with_its_followers(
        client, world, token, shop, office):
    wrong = wrong_phone()
    customer_issue = send(client, CREATE, wrong, token=token, park=True).json()["data"]
    order = booking_for(world, shop, wrong)

    # Sent without park: it still cannot run, so it waits.
    body = send(client, BOOK, order, token=token).json()

    assert body["code"] == "parked", body
    assert (body["data"]["waiting_on"], body["data"]["error"]) == (customer_issue["issue_id"], None)
    booking_issue = SyncIssue.objects.get(sync_id=order["sync_id"])
    assert booking_issue.reason == IssueReason.FOLLOWER
    assert not Order.objects.exists()
    # The order's own later calls line up behind the booking, as before.
    later = back(order)
    assert send(client, RETURN, later, token=token).json()["data"]["waiting_on"] == booking_issue.pk
    # The booking cannot be replayed or dismissed ahead of its customer.
    with pytest.raises(services.SyncActionError, match="waits behind"):
        services.replay(every_issue(), booking_issue.pk, user=office)
    # The office sees why.
    rows = _annotate(list(every_issue().order_by("id")))
    assert rows[0].order_no == "Mariam Saeed · 971500000000"
    assert (rows[1].waiting_on, rows[1].waiting_on_customer) == (customer_issue["issue_id"], True)
    # And the tablet, through the status call.
    status = {row["sync_id"]: row for row in send(client, STATUS, {}, token=token).json()["data"]["issues"]}
    assert status[order["sync_id"]]["waiting_on"] == customer_issue["issue_id"]
    assert status[wrong["sync_id"]]["customer_id"] is None

    outcome = services.replay(every_issue(), customer_issue["issue_id"], user=office,
                              request_data={**wrong, "full_number": "971509876543"})

    later_issue = SyncIssue.objects.get(sync_id=later["sync_id"])
    assert outcome == {"resolved": [customer_issue["issue_id"], booking_issue.pk, later_issue.pk],
                       "stopped_at": None}
    placed = Order.objects.get(pk=order["sync_id"])
    assert placed.customer == Customer.objects.get(sync_id=wrong["sync_id"])
    assert OrderItem.objects.get(pk=order["items"][0]["sync_id"]).status == OrderItemStatus.RETURNED
    assert not every_issue().filter(status=IssueStatus.OPEN).exists()
    # The tablet learns the customer's server id from the status call.
    status = {row["sync_id"]: row for row in send(client, STATUS, {}, token=token).json()["data"]["issues"]}
    assert status[wrong["sync_id"]]["customer_id"] == placed.customer_id


def test_a_booking_that_fails_after_its_customer_stays_open_with_its_error(client, world, token, shop, office):
    wrong = wrong_phone()
    customer_issue = send(client, CREATE, wrong, token=token, park=True).json()["data"]
    order = booking_for(world, shop, wrong)
    order["items"][0]["vehicle_id"] = 999999
    send(client, BOOK, order, token=token)

    outcome = services.replay(every_issue(), customer_issue["issue_id"], user=office,
                              request_data={**wrong, "full_number": "971509876543"})

    stopped = SyncIssue.objects.get(sync_id=order["sync_id"])
    assert (outcome["resolved"], outcome["stopped_at"]) == ([customer_issue["issue_id"]], stopped)
    assert (stopped.status, stopped.reason) == (IssueStatus.OPEN, IssueReason.REFUSED)
    assert Customer.objects.filter(sync_id=wrong["sync_id"]).exists()


def test_dismissing_the_customer_frees_its_bookings_for_the_office(client, world, token, shop, office):
    wrong = wrong_phone()
    customer_issue = send(client, CREATE, wrong, token=token, park=True).json()["data"]
    order = booking_for(world, shop, wrong)
    send(client, BOOK, order, token=token)

    assert services.dismiss(every_issue(), customer_issue["issue_id"], user=office,
                            reason="Registered on the web") == [customer_issue["issue_id"]]

    waiting = SyncIssue.objects.get(sync_id=order["sync_id"])
    assert waiting.status == IssueStatus.OPEN
    # Corrected to the customer the office registered, it runs.
    corrected = {key: value for key, value in order.items() if key != "customer_sync_id"}
    corrected["customer_id"] = shop["ahmed"].pk
    outcome = services.replay(every_issue(), waiting.pk, user=office, request_data=corrected)
    assert outcome["stopped_at"] is None and Order.objects.get().customer == shop["ahmed"]


def test_a_booking_for_a_customer_already_on_the_server_runs_at_once(client, world, token, shop):
    created = new_customer()
    assert send(client, CREATE, created, token=token).json()["code"] == "ok"

    body = send(client, BOOK, booking_for(world, shop, created), token=token).json()

    assert body["code"] == "ok", body
    assert body["data"]["customer"]["name"] == "Mariam Saeed"


def test_an_unknown_customer_sync_id_is_customer_not_synced_and_parks_with_park(client, world, token, shop):
    unknown = new_customer()

    response = send(client, BOOK, booking_for(world, shop, unknown), token=token)
    assert (response.status_code, response.json()["code"], response.json()["data"]["retry"]) == (
        409, "customer_not_synced", True)

    body = send(client, BOOK, booking_for(world, shop, unknown, number=232), token=token, park=True).json()
    assert (body["code"], body["data"]["error"]["code"]) == ("parked", "customer_not_synced")


def test_a_booking_refused_for_its_customer_runs_when_the_customer_is_replayed(
        client, world, token, shop, office):
    """The booking arrived first and was parked customer_not_synced; the
    create was parked after it. Replaying the create runs it too."""
    wrong = wrong_phone()
    order = booking_for(world, shop, wrong)
    send(client, BOOK, order, token=token, park=True)
    customer_issue = send(client, CREATE, wrong, token=token, park=True).json()["data"]

    outcome = services.replay(every_issue(), customer_issue["issue_id"], user=office,
                              request_data={**wrong, "full_number": "971509876543"})

    assert outcome["stopped_at"] is None and len(outcome["resolved"]) == 2
    assert Order.objects.filter(pk=order["sync_id"]).exists()


@pytest.mark.parametrize("both", [True, False], ids=["both", "neither"])
def test_a_booking_names_exactly_one_customer(client, world, token, shop, both):
    data = booking(world, shop)
    if both:
        data["customer_sync_id"] = str(uuid7())
    else:
        data.pop("customer_id")

    response = send(client, BOOK, data, token=token)

    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")
    assert "customer_id" in response.json()["data"]["errors"]
