"""Status Tracking (client, 20 Nov 2026): the old RMS grid -- Country / Emirate /
Station search; Rent / Test / Alert / Missing totals; per station Last updated,
Tags read, Vehicles, Available, In rent, In test, In alert, Missing (a star that
opens the missing vehicles) and Antenna -- and Reader CPU Approval's search and
switches."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.crew.models import Designation, DutyRoster, DutyRosterDayType, Employee, RosterCategory
from apps.tracking import views
from apps.tracking.models import (
    PresenceState,
    Reader,
    ReaderAssignment,
    TagLastSeen,
    VehiclePresence,
)
from apps.tracking.tests.test_screens import make_bike, post, reader  # noqa: F401
from core.timezones import business_date_for

pytestmark = pytest.mark.django_db


def presence(world, bike, state, *, heard=None, weak=False, branch=None):
    now = timezone.now()
    return VehiclePresence.objects.create(
        vehicle=bike, company=world["company"], branch=branch or world["adc1"], state=state, since=now,
        last_seen_at=heard, weak_since=now if weak else None)


@pytest.fixture
def station(world, reader):  # noqa: F811
    """ADC1's reader online; seven bikes, one of each case."""
    Reader.objects.filter(pk=reader.pk).update(online=True, last_heartbeat_at=timezone.now())
    now = timezone.now()
    cases = [
        ("AV1", "A0000001", PresenceState.PRESENT, now, False),                              # available
        ("AL1", "A0000002", PresenceState.PRESENT, now - datetime.timedelta(seconds=120), False),  # not heard 2 min
        ("AL2", "A0000003", PresenceState.PRESENT, now, True),                               # signal weakening
        ("MI1", "A0000004", PresenceState.MISSING, now - datetime.timedelta(minutes=20), False),
        ("RE1", "A0000005", PresenceState.ON_RENT, None, False),
        ("TE1", "A0000006", PresenceState.ON_TEST, None, False),
    ]
    bikes = {}
    for name, tag, state, heard, weak in cases:
        bike = make_bike(world, name, tag)
        presence(world, bike, state, heard=heard, weak=weak)
        if heard:
            TagLastSeen.objects.create(tag=tag, reader=reader, port=1, seen_at=heard, rssi=-40, rssi_avg=-40.0)
        bikes[name] = bike
    return bikes


def test_the_grid_has_the_old_rms_columns(client_in, station):
    page = client_in.get(reverse("tracking-status"))
    assert page.status_code == 200
    body = page.content.decode()
    for label in ("Status Tracking", "Tags read", "Available", "In rent", "In test", "In alert", "Missing", "Antenna"):
        assert label in body

    row = page.context["rows"][0]
    assert (row["vehicles"], row["available"], row["alert"], row["missing"], row["rent"], row["test"]) == (
        6, 1, 2, 1, 1, 1)
    assert row["tags_read"] == 3                      # heard by its own reader within the missing window
    assert row["antenna"] == "online" and row["last_updated"] is not None
    assert page.context["totals"] == {"rent": 1, "test": 1, "alert": 2, "missing": 1}
    assert reverse("tracking-status-missing", args=[row["id"]]) in body      # the star opens the dialog


def test_a_closed_station_shows_zero(client_in, station, monkeypatch):
    monkeypatch.setattr(views, "branch_open_state", lambda branch, now: "closed")
    row = client_in.get(reverse("tracking-status")).context["rows"][0]
    assert row["closed"] and row["vehicles"] == 6
    assert (row["available"], row["alert"], row["missing"], row["rent"], row["test"], row["tags_read"]) == (0,) * 6


def test_the_search_is_state_then_branch(client_in, world, station):
    """Client, 20 Nov 2026: no Country; State narrows Branch."""
    from apps.company.models import Branch, BranchType

    adc1 = world["adc1"]
    adc2 = Branch.objects.create(company=world["company"], location=adc1.location, short_code="ADC2",
                                 name="Abu Dhabi Corniche 2", branch_type=BranchType.STATION)
    presence(world, make_bike(world, "AD2", "A0000009", branch=adc2), PresenceState.PRESENT, branch=adc2)
    second = Reader.objects.create(device_id="190619101016200", company=world["company"], status="approved")
    ReaderAssignment.objects.create(reader=second, branch=adc2, from_at=timezone.now())
    page = client_in.get(reverse("tracking-status"))
    assert {r["id"] for r in page.context["rows"]} == {adc1.pk, adc2.pk}
    body = page.content.decode()
    assert 'name="country"' not in body and "countries" not in page.context
    assert ">State<" in body and ">Branch<" in body
    assert f'data-state="{adc1.location.state_id}"' in body          # each branch says its state

    one = client_in.get(reverse("tracking-status"), {"station": adc2.pk}).context
    assert [r["id"] for r in one["rows"]] == [adc2.pk]
    by_state = client_in.get(reverse("tracking-status"), {"state": adc1.location.state_id}).context["rows"]
    assert {r["id"] for r in by_state} == {adc1.pk, adc2.pk}
    assert client_in.get(reverse("tracking-status"), {"state": 999999}).context["rows"] == []


def test_each_station_row_carries_what_the_page_compares(client_in, world, station):
    body = client_in.get(reverse("tracking-status")).content.decode()
    adc1 = world["adc1"].pk
    assert f'data-station="{adc1}"' in body
    assert 'data-sig="3|6|1|1|1|2|1|online|False"' in body
    assert reverse("tracking-status-station", args=[adc1]) in body


def test_a_station_opens_its_vehicles_missing_first(client_in, world, station):
    fragment = client_in.get(reverse("tracking-status-station", args=[world["adc1"].pk]))
    assert fragment.status_code == 200
    names = [v["name"] for v in fragment.context["vehicles"]]
    assert names[0] == "MI1" and len(names) == 6
    assert 'class="is-missing"' in fragment.content.decode()
    assert client_in.get(reverse("tracking-status-station", args=[world["theirs"].pk])).status_code == 404
    page = client_in.get(reverse("tracking-status"), {"branch": world["adc1"].pk})      # the page still works
    assert page.status_code == 200 and len(page.context["vehicles"]) == 6


def test_a_pending_reader_has_approve_and_block_and_no_menu(client_in, world, reader):  # noqa: F811
    Reader.objects.create(device_id="190619101016999", status="pending", company=world["company"])
    body = client_in.get(reverse("tracking-readers")).content.decode()
    pending = body[body.index("190619101016999"):]
    pending = pending[:pending.index("</tr>")]
    assert ">Approve<" in pending and ">Block<" in pending and "data-scr-menu-toggle" not in pending
    approved = body[body.index(reader.device_id):]
    approved = approved[:approved.index("</tr>")]
    assert "data-scr-menu-toggle" in approved and ">Test<" in approved


def test_the_missing_dialog_lists_them_with_todays_cashiers(client_in, world, station):
    company = world["company"]
    designation = Designation.objects.filter(company=company).first() or Designation.objects.create(
        company=company, code="CSH", title="Cashier")
    cashier = Employee.objects.create(company=company, employee_code="CSH001", first_name="Riya",
                                      designation=designation)
    DutyRoster.objects.create(employee=cashier, branch=world["adc1"], date=business_date_for(company),
                              day_type=DutyRosterDayType.WORKING, role=RosterCategory.CASHIER,
                              shift1_start=timezone.now(), shift1_end=timezone.now() + datetime.timedelta(hours=8))

    page = client_in.get(reverse("tracking-status-missing", args=[world["adc1"].pk]))

    assert page.status_code == 200
    body = page.content.decode()
    assert "MI1" in body and "Byky" in body and "Monaco" in body       # number, category, type
    assert "AV1" not in body and "AL1" not in body
    assert "Riya" in body and "Primary" in body
    assert client_in.get(reverse("tracking-status-missing", args=[999999])).status_code == 404
    assert client_in.get(reverse("tracking-status-missing", args=[world["theirs"].pk])).status_code == 404


def test_the_pages_carry_the_old_names(client_in, reader):  # noqa: F811
    from apps.portal.models import Page

    assert Page.objects.get(code="tracking.status").name == "Status Tracking"
    assert Page.objects.get(code="tracking.readers").name == "Reader CPU Approval"
    body = client_in.get(reverse("tracking-readers")).content.decode()
    assert "Reader CPU Approval" in body
    assert "Search CPU ID" in body and f'data-search="{reader.device_id} ' in body
    assert 'data-scr-page-size="50"' in body


def test_both_reader_switches_turn_off_and_on(client_in, reader):  # noqa: F811
    for name, field in (("tracking-reader-enabled", "enabled"), ("tracking-reader-keep-raw", "keep_raw")):
        url = reverse(name, args=[reader.pk])
        assert post(client_in, url, {"enabled": False}).json()["ok"] is True
        reader.refresh_from_db()
        assert getattr(reader, field) is False, name
        assert post(client_in, url, {"enabled": True}).json()["ok"] is True
        reader.refresh_from_db()
        assert getattr(reader, field) is True, name


def test_only_stations_with_a_reader_mapped_are_listed(client_in, world, station):
    """Client, 20 Nov 2026: a station with tagged bikes but no reader is left out
    of the grid and the dropdowns; a mapped reader that is off still lists it."""
    from apps.company.models import Branch, BranchType

    bare = Branch.objects.create(company=world["company"], location=world["adc1"].location, short_code="BARE",
                                 name="No Reader Park", branch_type=BranchType.STATION)
    presence(world, make_bike(world, "NR1", "A0000010", branch=bare), PresenceState.MISSING, branch=bare)
    page = client_in.get(reverse("tracking-status"))
    assert [r["id"] for r in page.context["rows"]] == [world["adc1"].pk]
    assert "No Reader Park" not in page.content.decode()
    assert page.context["totals"]["missing"] == 1                  # its missing bike is not counted

    Reader.objects.filter(device_id="190619101016089").update(enabled=False, online=False)
    rows = client_in.get(reverse("tracking-status")).context["rows"]
    assert [(r["id"], r["antenna"]) for r in rows] == [(world["adc1"].pk, "offline")]
