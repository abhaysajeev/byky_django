"""POST /api/v1/operator/device/branch: which station a tablet is mapped to,
before login -- by its own installation_id, with the registration number as
an optional cross-check."""

import datetime

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.company.models import BranchWorkingTime, WeekDay
from apps.devices.models import Device, DeviceMapping, DeviceStatus
from apps.devices.tests.test_api_registration import approved_device, register
from core.enums import Channel

URL = "/api/v1/operator/device/branch"


@pytest.fixture(autouse=True)
def fresh_throttle():
    cache.clear()
    yield
    cache.clear()


def ask(client, installation_id="install-1", app="operator", **extra):
    response = client.post(
        f"/api/v1/{app}/device/branch",
        {"credentials": {}, "request_data": {"installation_id": installation_id, **extra}},
        content_type="application/json",
    )
    return response.status_code, response.json()


@pytest.fixture
def station(branch):
    return branch


def mapped(company, station, **kw):
    device = approved_device(company, **kw)
    DeviceMapping.objects.create(device=device, branch=station, from_date=timezone.now())
    return device


def test_an_approved_mapped_tablet_learns_its_station(client, company, station):
    device = mapped(company, station)

    code, body = ask(client, device_registration_id=device.device_registration_id)

    assert (code, body["code"]) == (200, "ok")
    assert body["data"]["device"] == {"device_registration_id": device.device_registration_id,
                                      "name": "AlMamzar-POS1", "status": "approved"}
    branch = body["data"]["branch"]
    assert (branch["branch_id"], branch["branch_code"], branch["name"], branch["location"], branch["state"],
            branch["is_active"]) == (station.pk, "AUH01", "Corniche 1", "Corniche", "Abu Dhabi", True)
    assert branch["mapped_since"]


def test_without_a_station_it_says_so(client, company):
    device = approved_device(company)

    code, body = ask(client)

    assert (code, body["code"], body["data"]["device"]["device_registration_id"]) == (
        409, "device_not_mapped", device.device_registration_id)


def test_a_waiting_tablet_gets_no_station(client, company):
    number = register(client, "install-1").json()["data"]["device_registration_id"]

    code, body = ask(client)

    assert (code, body["code"], body["data"]) == (202, "device_pending_approval", {"device_registration_id": number})


def test_a_reinstall_waiting_keeps_its_number_and_gets_no_station(client, company, station):
    device = mapped(company, station, installation_id="old-install")
    register(client, "new-install", platform_id="fed25")

    code, body = ask(client, "new-install", device_registration_id=device.device_registration_id)

    assert (code, body["code"], body["data"]) == (
        202, "device_reconnect_pending", {"device_registration_id": device.device_registration_id})
    assert ask(client, "old-install")[1]["code"] == "ok"           # the current install still works


@pytest.mark.parametrize(("status", "code"), [(DeviceStatus.BLOCKED, "device_blocked"),
                                              (DeviceStatus.RETIRED, "device_retired")])
def test_a_blocked_or_retired_tablet_gets_no_station(client, company, station, status, code):
    mapped(company, station, status=status)

    assert ask(client)[1]["code"] == code


def test_an_unknown_install_or_a_wrong_number_is_not_registered(client, company, station):
    device = mapped(company, station)

    assert ask(client, "never-seen")[1]["code"] == "device_not_registered"
    assert ask(client, device_registration_id=device.device_registration_id + 1)[1]["code"] == (
        "device_not_registered")


def test_a_closed_station_is_flagged(client, company, station):
    mapped(company, station)
    station.is_active = False
    station.save()

    assert ask(client)[1]["data"]["branch"]["is_active"] is False


def test_only_the_operator_app(client, company, station):
    mapped(company, station)
    Device.objects.update(channel=Channel.OPERATOR)

    assert ask(client, app="manager")[1]["code"] == "wrong_channel"


def test_installation_id_is_required(client, db):
    code, body = ask(client, installation_id="")

    assert (code, body["code"]) == (400, "invalid_request")


def test_the_call_is_in_the_api_docs(client, db):
    assert "/api/v1/{app}/device/branch" in client.get("/api/schema/").content.decode()


def test_the_working_time_of_the_asked_day(client, company, station):
    mapped(company, station)
    BranchWorkingTime.objects.create(branch=station, week_day=WeekDay.FRIDAY, shift_number=2,
                                     start_time=datetime.time(16), end_time=datetime.time(23, 59))
    BranchWorkingTime.objects.create(branch=station, week_day=WeekDay.FRIDAY, shift_number=1,
                                     start_time=datetime.time(8), end_time=datetime.time(13))
    BranchWorkingTime.objects.create(branch=station, week_day=WeekDay.SATURDAY, shift_number=1,
                                     start_time=datetime.time(9), end_time=datetime.time(17))

    friday = ask(client, date="2026-10-09")[1]["data"]["working_time"]
    sunday = ask(client, date="2026-10-11")[1]["data"]["working_time"]

    assert (friday["date"], friday["week_day"], friday["week_day_name"], friday["is_set"]) == (
        "2026-10-09", WeekDay.FRIDAY, "Friday", True)
    assert friday["shifts"] == [{"shift_number": 1, "start": "08:00", "end": "13:00"},
                                {"shift_number": 2, "start": "16:00", "end": "23:59"}]
    assert (sunday["shifts"], sunday["is_set"]) == ([], True)          # no shift that day: closed
    assert friday["open_now"] in ("open", "closed")


def test_no_working_time_at_all_is_not_set(client, company, station):
    mapped(company, station)

    working = ask(client)[1]["data"]["working_time"]

    assert (working["is_set"], working["shifts"], working["open_now"]) == (False, [], "not_set")
    assert working["date"]                                               # today, company time


def test_a_bad_date_is_refused(client, company, station):
    mapped(company, station)

    code, body = ask(client, date="09-10-2026")

    assert (code, body["code"]) == (400, "invalid_request") and "date" in body["data"]["errors"]
