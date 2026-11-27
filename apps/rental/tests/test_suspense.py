"""Suspense entries (owner, 26 Nov 2026; apps/rental/suspense.py): a branch's
collection came in short or in excess. A positive amount and a type; suspected
orders optional and of that branch and business day; edited while Open, never
deleted (Cancel, with a reason); Resolved is a label only. Web only, by page
privilege (rental.suspense: Create, Read, Update, Delete = Cancel, Approve =
Resolve)."""

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.company.models import Branch, BranchType, CompanyConfig
from apps.portal.models import Page
from apps.rental.models import SuspenseEntry, SuspenseStatus
from apps.rental.tests import test_order_screens as order_screens
from apps.rental.tests.conftest import post
from apps.rental.tests.test_views import revoke
from core.ids import uuid7

at, make_order, shop = order_screens.at, order_screens.make_order, order_screens.shop

LIST = "/rental/suspense/list/"
SAVE = "/rental/suspense/save/"
ORDERS = "/rental/suspense/orders/"


def day_label(day):
    return f"{day:%d %b %Y}"


def entry_data(world, **changes):
    values = {"branch": world["adc1"].pk, "entry_date": day_label(at(-1, world=world).date()),
              "kind": "short", "amount": "30", "remarks": "Cash count 30 short.", "suspected_orders": []}
    values.update(changes)
    return values


def add(client_in, world, **changes):
    return post(client_in, SAVE, entry_data(world, **changes))


def only_entry():
    return SuspenseEntry.objects.get()


# -- The record ---------------------------------------------------------------------------


def test_an_entry_saves_with_its_type_and_positive_amount(client_in, world):
    body = add(client_in, world, amount="30.5").json()

    assert body["ok"] is True, body
    entry = only_entry()
    assert (entry.branch, entry.kind, entry.amount, entry.status) == (
        world["adc1"], "short", Decimal("30.5"), SuspenseStatus.OPEN)
    assert entry.entry_date == at(-1, world=world).date()
    assert entry.signed_amount == Decimal("-30.5")
    assert entry.created_by.username == "sara.k"


@pytest.mark.parametrize("changes, field", [
    ({"amount": "0"}, "Amount"), ({"amount": "-5"}, "Amount"), ({"amount": "abc"}, "Amount"),
    ({"amount": "1.2345"}, "Amount"),                        # more decimals than the company's money
    ({"kind": "lost"}, "Type"), ({"entry_date": ""}, "Date"), ({"branch": ""}, "Branch"),
])
def test_bad_values_are_refused_by_field(client_in, world, changes, field):
    response = add(client_in, world, **changes)

    assert response.status_code == 400
    assert field in [e["field"] for e in response.json()["errors"]]
    assert not SuspenseEntry.objects.exists()


def test_a_future_date_and_another_companys_branch_are_refused(client_in, world):
    tomorrow = day_label(at(1, world=world).date())
    errors = add(client_in, world, entry_date=tomorrow, branch=world["theirs"].pk).json()["errors"]
    assert {e["field"] for e in errors} == {"Branch"}        # the date is checked once the branch is known
    errors = add(client_in, world, entry_date=tomorrow).json()["errors"]
    assert errors == [{"field": "Date", "message": "The date cannot be in the future."}]


def test_the_database_keeps_amounts_positive_and_statuses_consistent(world):
    base = {"company": world["company"], "branch": world["adc1"], "entry_date": datetime.date(2026, 11, 20),
            "kind": "short"}
    with pytest.raises(IntegrityError), transaction.atomic():
        SuspenseEntry.objects.create(id=uuid7(), amount=0, **base)
    with pytest.raises(IntegrityError), transaction.atomic():
        SuspenseEntry.objects.create(id=uuid7(), amount=5, status="resolved", **base)       # no resolved_at
    with pytest.raises(IntegrityError), transaction.atomic():
        SuspenseEntry.objects.create(id=uuid7(), amount=5, status="cancelled", **base)      # no reason


# -- Suspected orders ------------------------------------------------------------------------


def test_suspected_orders_are_optional_and_of_that_branch_and_day(client_in, world, shop):
    yesterday = make_order(world, shop, "ORD-1", day=-1)
    today = make_order(world, shop, "ORD-2", day=0, lines=("returned",))   # the bike is out on ORD-1

    assert add(client_in, world, suspected_orders=[]).json()["ok"] is True
    body = add(client_in, world, suspected_orders=[str(yesterday.pk)]).json()
    assert body["ok"] is True
    assert list(SuspenseEntry.objects.get(pk=body["pk"]).suspected_orders.all()) == [yesterday]

    refused = add(client_in, world, suspected_orders=[str(today.pk)]).json()
    assert refused["errors"] == [{"field": "Suspected orders",
                                  "message": "Pick only orders booked at this branch on this date."}]


def test_an_order_after_midnight_counts_for_the_business_day_before(client_in, world, shop):
    CompanyConfig.objects.update_or_create(company=world["company"], defaults={"report_day_start": datetime.time(4)})
    order = make_order(world, shop, "ORD-NIGHT", day=0)
    night = at(0, 2, 0, world=world)                         # 02:00 today belongs to yesterday
    type(order).objects.filter(pk=order.pk).update(booked_at=night, start_time=night)
    yesterday = day_label(at(-1, world=world).date())

    offered = client_in.get(ORDERS, {"branch": world["adc1"].pk, "date": yesterday}).json()["orders"]
    assert [o["name"].split(" · ")[0] for o in offered] == ["ORD-NIGHT"]
    assert offered[0]["name"].split(" · ")[1] == "02:00"
    today = day_label(at(0, world=world).date())
    assert client_in.get(ORDERS, {"branch": world["adc1"].pk, "date": today}).json()["orders"] == []


def test_the_orders_endpoint_is_scoped_and_needs_read(client_in, world, shop):
    make_order(world, shop, "ORD-1", day=-1)
    yesterday = day_label(at(-1, world=world).date())

    assert client_in.get(ORDERS, {"branch": world["theirs"].pk, "date": yesterday}).json()["orders"] == []
    assert client_in.get(ORDERS, {"branch": "", "date": yesterday}).json()["orders"] == []
    revoke(client_in, "read", page="rental.suspense")
    assert client_in.get(ORDERS, {"branch": world["adc1"].pk, "date": yesterday}).status_code == 403


# -- Edit, Resolve, Cancel ----------------------------------------------------------------


def test_edit_only_while_open(client_in, world):
    pk = add(client_in, world).json()["pk"]

    edited = post(client_in, SAVE, entry_data(world, pk=pk, kind="excess", amount="12.5")).json()
    assert edited["ok"] is True
    assert (only_entry().kind, only_entry().amount) == ("excess", Decimal("12.5"))

    post(client_in, f"/rental/suspense/{pk}/resolve/", {"remarks": "Fixed"})
    refused = post(client_in, SAVE, entry_data(world, pk=pk, amount="1")).json()
    assert refused["errors"] == [{"field": "Status", "message": "Only an open entry can be edited."}]


def test_resolve_is_a_label_with_who_and_when(client_in, world, shop):
    order = make_order(world, shop, "ORD-1", day=-1)
    pk = add(client_in, world, suspected_orders=[str(order.pk)]).json()["pk"]
    before = type(order).objects.values().get(pk=order.pk)

    body = post(client_in, f"/rental/suspense/{pk}/resolve/", {"remarks": "Adjusted by hand"}).json()

    entry = only_entry()
    assert body["ok"] is True and body["redirect"] == f"/rental/suspense/{pk}/"
    assert (entry.status, entry.resolution_remarks, entry.resolved_by.username) == (
        "resolved", "Adjusted by hand", "sara.k")
    assert entry.resolved_at is not None
    assert type(order).objects.values().get(pk=order.pk) == before      # no order touched
    again = post(client_in, f"/rental/suspense/{pk}/resolve/", {}).json()
    assert again["errors"][0]["message"] == "Only an open entry can be resolved."


def test_cancel_needs_a_reason_and_is_never_a_delete(client_in, world):
    pk = add(client_in, world).json()["pk"]

    refused = post(client_in, f"/rental/suspense/{pk}/cancel/", {"reason": "  "}).json()
    assert refused["errors"] == [{"field": "Reason", "message": "Say why the entry is cancelled."}]
    assert post(client_in, f"/rental/suspense/{pk}/cancel/", {"reason": "Counted twice"}).json()["ok"] is True

    entry = only_entry()
    assert (entry.status, entry.cancel_reason, entry.cancelled_by.username) == ("cancelled", "Counted twice", "sara.k")
    assert post(client_in, f"/rental/suspense/{pk}/resolve/", {}).json()["ok"] is False


@pytest.mark.parametrize("action, url, payload", [
    ("create", SAVE, None),
    ("update", SAVE, "edit"),
    ("approve", "/rental/suspense/{pk}/resolve/", {}),
    ("delete", "/rental/suspense/{pk}/cancel/", {"reason": "x"}),
])
def test_each_action_needs_its_privilege(client_in, world, action, url, payload):
    pk = add(client_in, world).json()["pk"]
    revoke(client_in, action, page="rental.suspense")
    if payload is None:
        payload = entry_data(world)
    elif payload == "edit":
        payload = entry_data(world, pk=pk)

    response = post(client_in, url.format(pk=pk), payload)

    assert response.status_code == 403
    assert SuspenseEntry.objects.count() == 1 and only_entry().status == "open"


def test_another_companys_entry_is_invisible(client_in, world):
    theirs = SuspenseEntry.objects.create(
        id=uuid7(), company=world["other"], branch=world["theirs"], entry_date=datetime.date(2026, 11, 20),
        kind="excess", amount=5)

    assert client_in.get(f"/rental/suspense/{theirs.pk}/").status_code == 404
    assert post(client_in, f"/rental/suspense/{theirs.pk}/resolve/", {}).status_code == 404
    assert client_in.get(LIST, {"status": "all"}).context["rows"] == []


# -- Screens ------------------------------------------------------------------------------


def test_the_page_is_registered_under_rental_with_its_actions(db):
    page = Page.objects.get(code="rental.suspense")
    assert (page.module.code, page.name) == ("rental", "Suspense Entries")
    assert set(page.actions) == {"create", "read", "update", "delete", "approve"}


def test_the_list_shows_open_entries_in_both_views_and_filters(client_in, world, shop):
    order = make_order(world, shop, "ORD-77", day=-1)
    add(client_in, world, suspected_orders=[str(order.pk)])
    done = add(client_in, world, kind="excess", amount="5").json()["pk"]
    post(client_in, f"/rental/suspense/{done}/resolve/", {})

    response = client_in.get(LIST)
    html = response.content.decode()
    assert [r["kind"] for r in response.context["rows"]] == ["short"]            # Open by default
    assert 'data-sus-pane="list"' in html and 'data-sus-pane="grid"' in html
    assert "ORD-77" in html and "−30.00" in html
    assert "Suspense Entries" in html                                           # in the sidebar too
    assert len(client_in.get(LIST, {"status": "all"}).context["rows"]) == 2
    assert [r["kind"] for r in client_in.get(LIST, {"status": "all", "kind": "excess"}).context["rows"]] == ["excess"]
    assert client_in.get(LIST, {"status": "all", "q": "ORD-77"}).context["rows"][0]["orders"][0]["no"] == "ORD-77"


def test_the_detail_shows_its_buttons_by_privilege(client_in, world):
    pk = add(client_in, world).json()["pk"]

    html = client_in.get(f"/rental/suspense/{pk}/").content.decode()
    assert "Mark resolved" in html and "Cancel entry" in html and 'data-scr-open="suspense:edit"' in html

    revoke(client_in, "approve", "delete", "update", page="rental.suspense")
    html = client_in.get(f"/rental/suspense/{pk}/").content.decode()
    assert "Mark resolved" not in html and "Cancel entry" not in html
    assert 'data-scr-open="suspense:edit"' not in html


def test_the_list_needs_read(client_in):
    revoke(client_in, "read", page="rental.suspense")
    assert client_in.get(LIST).status_code == 403


def test_a_branch_of_another_company_is_not_offered(client_in, world):
    Branch.objects.create(company=world["other"], location=world["adc1"].location, short_code="OTH2",
                          name="Their Second", branch_type=BranchType.STATION)
    names = [b["name"] for b in client_in.get(LIST).context["suspense_branches_list"]]
    assert names == ["Abu Dhabi Corniche 1"]
