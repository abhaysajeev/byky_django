"""The Tracking screens, settings, readers, calibration and the bell."""

import datetime
from decimal import Decimal

import pytest
from django.db import OperationalError, connection
from django.urls import reverse
from django.utils import timezone

from apps.company.models import Branch, BranchType
from apps.fleet.models import UOM, Brand, Category, Vehicle, VehicleType
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.tracking import bell, calibration, services, upkeep, views
from apps.tracking.models import (
    AlertRead,
    Antenna,
    BranchTrackingSettings,
    Calibration,
    CalibrationStatus,
    EventKind,
    PointZone,
    PresenceEvent,
    PresenceState,
    Reader,
    ReaderAssignment,
    ReaderStatus,
    TagLastSeen,
    TrackingSettings,
)
from core.enums import Channel
from core.models import User, UserScope

pytestmark = pytest.mark.django_db

SCREENS = ["tracking-status", "tracking-alerts", "tracking-readers", "tracking-antennas",
           "tracking-settings", "tracking-privileges"]
NOW = datetime.datetime(2026, 11, 9, 8, 0, tzinfo=datetime.UTC)


@pytest.fixture
def reader(world):
    r = Reader.objects.create(device_id="190619101016089", company=world["company"], name="ADC1",
                              status=ReaderStatus.APPROVED, enabled=True)
    ReaderAssignment.objects.create(reader=r, branch=world["adc1"], from_at=NOW)
    return r


@pytest.mark.parametrize("name", SCREENS)
def test_every_screen_opens_empty(client_in, name):
    assert client_in.get(reverse(name)).status_code == 200


@pytest.mark.parametrize("name", SCREENS)
def test_every_screen_opens_with_a_reader(client_in, reader, name):
    assert client_in.get(reverse(name)).status_code == 200


@pytest.mark.parametrize("name", SCREENS)
def test_no_privilege_no_screen(client, name):
    assert client.get(reverse(name)).status_code in (302, 403)


# -- settings ---------------------------------------------------------------------------------

def post(client, url, payload):
    return client.post(url, data=payload, content_type="application/json")


def body(branch=None, **default):
    values = {"missing_after_s": 300, "offline_after_s": 300, **default}
    return {"retention_days": 3, "allowance_out_db": 3, "default": values, "branches": branch or []}


def save(client, world, payload):
    return post(client, reverse("tracking-settings-save", args=[world["company"].pk]), payload)


def test_saves_company_default_and_per_branch_overrides(client_in, world):
    response = save(client_in, world, body([
        {"branch": world["adc1"].pk, "alerts_on": True, "missing_after_s": "120", "offline_after_s": ""}],
        missing_after_s=200))

    assert response.json()["ok"] is True
    assert TrackingSettings.objects.get(company=world["company"]).missing_after_s == 200
    row = BranchTrackingSettings.objects.get(branch=world["adc1"])
    assert (row.alerts_on, row.missing_after_s, row.offline_after_s) == (True, 120, None)


@pytest.mark.parametrize("default,branch,field", [
    ({"missing_after_s": 20}, {}, "Company default: Missing after"),
    ({"offline_after_s": 60}, {}, "Company default: Reader offline after"),
    ({}, {"missing_after_s": "9999"}, "Abu Dhabi Corniche 1: Missing after"),
    ({}, {"offline_after_s": "30"}, "Abu Dhabi Corniche 1: Reader offline after"),
], ids=["too-short", "offline-below-a-heartbeat", "too-long", "station-offline-below-a-heartbeat"])
def test_refused_settings_save_nothing(client_in, world, default, branch, field):
    response = save(client_in, world, body([{"branch": world["adc1"].pk, **branch}], **default))

    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == field
    assert not TrackingSettings.objects.exists() and not BranchTrackingSettings.objects.exists()


def test_the_allowance_is_saved_within_its_range(client_in, world):
    assert save(client_in, world, {**body(), "allowance_out_db": 5}).json()["ok"] is True
    assert TrackingSettings.objects.get(company=world["company"]).allowance_out_db == 5
    assert save(client_in, world, {**body(), "allowance_out_db": 40}).status_code == 400


def test_every_active_station_is_listed_with_search_and_a_pager(client_in, world):
    html = client_in.get(reverse("tracking-settings")).content.decode()

    assert f'data-branch-row="{world["adc1"].pk}"' in html and 'class="scr-search"' in html and 'class="scr-pager"' in html
    assert "abu dhabi corniche 1" in html       # the lower-case search text
    assert "Alert after" not in html


def test_another_companys_settings_cannot_be_saved(client_in, world):
    response = post(client_in, reverse("tracking-settings-save", args=[world["other"].pk]), body())

    assert response.status_code == 404


# -- readers ----------------------------------------------------------------------------------

def test_approve_gives_a_station_switches_on_and_makes_four_ports(world):
    pending = Reader.objects.create(device_id="X1")

    services.approve(pending, company=world["company"], branch=world["adc1"], name="Gate", now=NOW)

    pending.refresh_from_db()
    assert (pending.status, pending.enabled, pending.name) == (ReaderStatus.APPROVED, True, "Gate")
    assert ReaderAssignment.objects.get(reader=pending, to_at__isnull=True).branch == world["adc1"]
    assert Antenna.objects.filter(reader=pending).count() == 4


def test_a_new_station_closes_the_old_assignment(world, reader):
    second = Branch.objects.create(company=world["company"], location=world["adc1"].location, short_code="ADC9",
                                   name="Nine", branch_type=BranchType.STATION)

    services.assign(reader, second, now=NOW + datetime.timedelta(hours=1))

    assert ReaderAssignment.objects.filter(reader=reader, to_at__isnull=True).get().branch == second
    assert ReaderAssignment.objects.filter(reader=reader).count() == 2


def add(client, world, device="190619101016097", branch=None):
    return post(client, reverse("tracking-reader-add"), {
        "device_id": device, "name": "Gate", "branch": (branch or world["adc1"]).pk})


def test_save_approves_a_new_reader_and_gives_it_ports(client_in, world):
    assert add(client_in, world).json()["ok"] is True

    reader = Reader.objects.get()
    assert (reader.status, reader.enabled, reader.company) == (ReaderStatus.APPROVED, True, world["company"])
    assert Antenna.objects.filter(reader=reader).count() == 4


def test_save_adopts_a_reader_already_waiting_as_pending(client_in, world):
    waiting = Reader.objects.create(device_id="190619101016097", last_ip="94.200.1.2")

    assert add(client_in, world).json()["ok"] is True

    waiting.refresh_from_db()
    assert waiting.status == ReaderStatus.APPROVED and str(waiting.last_ip) == "94.200.1.2"


@pytest.mark.parametrize("fields", [{"status": ReaderStatus.APPROVED}, {"status": ReaderStatus.BLOCKED}],
                         ids=["approved", "blocked"])
def test_save_refuses_an_approved_or_blocked_reader(client_in, world, fields):
    Reader.objects.create(device_id="190619101016097", company=world["company"], **fields)

    assert add(client_in, world).status_code == 400


def test_save_refuses_another_companys_pending_reader(world):
    Reader.objects.create(device_id="190619101016097", company=world["other"])

    with pytest.raises(services.TrackingError, match="another company"):
        services.save_new_reader("190619101016097", branch=world["adc1"])


def test_keep_raw_can_be_switched(client_in, reader):
    assert post(client_in, reverse("tracking-reader-keep-raw", args=[reader.pk]), {"enabled": True}).json()["ok"] is True
    reader.refresh_from_db()
    assert reader.keep_raw is True


# -- Test connection --------------------------------------------------------------------------

def report_url(device):
    return reverse("tracking-reader-test") + f"?device_id={device}"


def test_a_healthy_reader(client_in, reader, monkeypatch):
    monkeypatch.setattr(services, "gateway_status", lambda: {"running": True, "enabled": True, "mode": "capture",
                                                             "db": True, "ok": True})
    Reader.objects.filter(pk=reader.pk).update(last_heartbeat_at=timezone.now(), last_data_at=timezone.now())

    data = client_in.get(report_url(reader.device_id)).json()

    assert data["healthy"] is True and data["connected"] and data["sending"]


def test_a_reader_never_heard(client_in, monkeypatch):
    monkeypatch.setattr(services, "gateway_status", lambda: {"running": True, "enabled": True, "mode": "capture",
                                                             "db": True, "ok": True})

    data = client_in.get(report_url("190619101016065")).json()

    assert data["healthy"] is False and data["reader"]["known"] is False


def test_connected_but_sending_no_tags_is_not_healthy(client_in, reader, monkeypatch):
    monkeypatch.setattr(services, "gateway_status", lambda: {"running": True, "enabled": True, "mode": "capture",
                                                             "db": True, "ok": True})
    Reader.objects.filter(pk=reader.pk).update(last_heartbeat_at=timezone.now())

    data = client_in.get(report_url(reader.device_id)).json()

    assert (data["connected"], data["sending"], data["healthy"]) == (True, False, False)


def test_the_gateway_being_down_is_reported_not_raised(client_in, settings):
    settings.TRACKING_GATEWAY_HEALTH_URL = "http://127.0.0.1:9/"

    data = client_in.get(report_url("190619101016065")).json()

    assert data["gateway"] == {"running": False, "enabled": False, "mode": "", "db": False, "ok": False}


def test_another_companys_reader_looks_unknown(client_in, world, monkeypatch):
    monkeypatch.setattr(services, "gateway_status", lambda: {"running": False, "enabled": False, "mode": "",
                                                             "db": False, "ok": False})
    Reader.objects.create(device_id="THEIRS1", company=world["other"], status=ReaderStatus.APPROVED,
                          last_heartbeat_at=timezone.now())

    assert client_in.get(report_url("THEIRS1")).json()["reader"]["known"] is False


def test_online_is_right_in_capture_mode(client_in, reader):
    """The engine does not run in capture mode, so Reader.online stays false."""
    Reader.objects.filter(pk=reader.pk).update(last_heartbeat_at=timezone.now(), online=False)

    assert ">Online<" in client_in.get(reverse("tracking-readers")).content.decode()


def test_block_switches_off(reader):
    services.block(reader)

    assert (reader.status, reader.enabled) == (ReaderStatus.BLOCKED, False)


def test_a_company_user_does_not_see_another_companys_reader(client_in, world):
    Reader.objects.create(device_id="THEIRS1", company=world["other"], status=ReaderStatus.APPROVED)

    assert b"THEIRS1" not in client_in.get(reverse("tracking-readers")).content


def test_a_reader_of_another_company_cannot_be_acted_on(client_in, world):
    theirs = Reader.objects.create(device_id="THEIRS1", company=world["other"], status=ReaderStatus.APPROVED)

    assert post(client_in, reverse("tracking-reader-block", args=[theirs.pk]), {}).status_code == 404


# -- calibration ------------------------------------------------------------------------------

def make_bike(world, name, tag, branch=None):
    company = world["company"]
    category = Category.objects.get_or_create(company=company, category_code="BYKY", defaults={"category_name": "Byky"})[0]
    brand = Brand.objects.get_or_create(company=company, brand_code="BYK", defaults={"brand_name": "Byky"})[0]
    kind = VehicleType.objects.get_or_create(company=company, vehicle_type_name="Monaco", defaults={
        "category": category, "brand": brand, "tax_percentage": Decimal("5.00")})[0]
    uom = UOM.objects.get_or_create(company=company, uom_code="NO", defaults={"uom_name": "Number"})[0]
    return Vehicle.objects.create(company=company, vehicle_code=name, vehicle_name=name, vehicle_type=kind, uom=uom,
                                  current_branch=branch or world["adc1"], rfid_epc=tag)


@pytest.fixture
def parked(world, reader, settings):
    """A connected reader in write mode, hearing two tagged bikes."""
    settings.TRACKING_MODE = "write"
    Reader.objects.filter(pk=reader.pk).update(last_heartbeat_at=timezone.now())
    reader.refresh_from_db()
    bikes = [make_bike(world, "MO 41", "170732EA"), make_bike(world, "MO 42", "1707365C")]
    for bike in bikes:
        TagLastSeen.objects.create(tag=bike.rfid_epc, reader=reader, port=1, seen_at=timezone.now(), rssi=-40, rssi_avg=-40.0)
    return {"reader": reader, "bikes": bikes}


def _session(reader, tags=("170732EA",)):
    return Calibration.objects.create(reader=reader, tags=list(tags), started_at=NOW)


def _uploads(tag="170732EA", values=(), port=1):
    """One read per upload, a second apart."""
    return [(tag, port, rssi, NOW + datetime.timedelta(seconds=n)) for n, rssi in enumerate(values)]


def test_a_good_start_records_one_boundary_point(parked):
    session = calibration.start(parked["reader"], [b.pk for b in parked["bikes"]])

    assert sorted(session.tags) == ["170732EA", "1707365C"]
    assert session.points.get().zone == PointZone.BOUNDARY


def test_a_bike_with_no_such_tag_at_this_station_is_refused(world, parked):
    elsewhere = Branch.objects.create(company=world["company"], location=world["adc1"].location, short_code="ADC9",
                                      name="Nine", branch_type=BranchType.STATION)
    away = make_bike(world, "MO 99", "AAAAAAAA", branch=elsewhere)

    with pytest.raises(calibration.CalibrationError, match="this reader's station"):
        calibration.start(parked["reader"], [away.pk])


def test_a_bike_the_reader_has_not_heard_is_named(world, parked):
    quiet = make_bike(world, "MO 77", "BBBBBBBB")

    with pytest.raises(calibration.CalibrationError, match="MO 77"):
        calibration.start(parked["reader"], [quiet.pk])


def test_a_bike_on_rent_is_refused(parked, monkeypatch):
    bike = parked["bikes"][0]
    monkeypatch.setattr(calibration, "active_rentals", lambda ids: {bike.pk: ("A1", NOW)})

    with pytest.raises(calibration.CalibrationError, match="MO 41"):
        calibration.start(parked["reader"], [bike.pk])


def test_no_bikes_is_refused(parked):
    with pytest.raises(calibration.CalibrationError, match="at least one"):
        calibration.start(parked["reader"], [])


def test_capture_mode_is_refused(parked, settings):
    settings.TRACKING_MODE = "capture"

    with pytest.raises(calibration.CalibrationError, match="capture"):
        calibration.start(parked["reader"], [parked["bikes"][0].pk])


def test_an_offline_reader_is_refused(parked):
    Reader.objects.filter(pk=parked["reader"].pk).update(last_heartbeat_at=timezone.now() - datetime.timedelta(hours=1))
    parked["reader"].refresh_from_db()

    with pytest.raises(calibration.CalibrationError, match="not connected"):
        calibration.start(parked["reader"], [parked["bikes"][0].pk])


def test_one_session_at_a_time_per_reader(parked):
    calibration.start(parked["reader"], [parked["bikes"][0].pk])

    with pytest.raises(calibration.CalibrationError, match="already"):
        calibration.start(parked["reader"], [parked["bikes"][1].pk])


def test_limits_are_the_typical_boundary_and_the_weakest_less_the_allowance(reader):
    TrackingSettings.objects.create(company=reader.company, allowance_out_db=3)
    # ten steady samples after the warm-up
    rows = _uploads(values=[-50] * 13)

    port = calibration.propose(_session(reader), rows)["ports"]["1"]

    assert (port["enter_rssi"], port["leave_rssi"]) == (-50, -53)
    assert port["source"] == "own"


def test_the_allowance_setting_moves_only_the_leave_limit(reader):
    TrackingSettings.objects.create(company=reader.company, allowance_out_db=6)

    port = calibration.propose(_session(reader), _uploads(values=[-50] * 13))["ports"]["1"]

    assert (port["enter_rssi"], port["leave_rssi"]) == (-50, -56)


def test_the_strongest_antenna_of_each_upload_counts_as_the_engine_does(reader):
    strong = _uploads(values=[-40] * 13, port=1)
    weak = _uploads(values=[-70] * 13, port=2)           # the same uploads, heard faintly on port 2

    ports = calibration.propose(_session(reader), strong + weak)["ports"]

    assert ports["1"]["source"] == "own" and ports["1"]["enter_rssi"] == -40
    assert ports["2"]["samples"] == 0


def test_an_antenna_with_too_few_samples_takes_the_combined_limits(reader):
    ports = calibration.propose(_session(reader), _uploads(values=[-50] * 13))["ports"]

    assert ports["3"]["source"] == "pooled" and ports["3"]["enter_rssi"] == ports["1"]["enter_rssi"]


def test_smoothing_is_the_same_as_the_gateways(reader):
    points = calibration.series(_uploads(values=[-40, -50]))["170732EA"]

    assert points[0][1] == -40 and points[1][1] == pytest.approx(0.3 * -50 + 0.7 * -40)


def test_no_usable_readings_cannot_be_approved(reader):
    proposal = calibration.propose(_session(reader), _uploads(values=[-50, -50]))     # all warm-up

    assert proposal["can_approve"] is False


def test_finish_is_refused_too_early_and_with_too_few_reads(reader, monkeypatch):
    session = _session(reader)
    with pytest.raises(calibration.CalibrationError, match="parked"):
        calibration.finish(session, now=NOW + datetime.timedelta(seconds=60))

    monkeypatch.setattr(calibration, "_rows", lambda c, end: _uploads(values=[-50] * 3))
    with pytest.raises(calibration.CalibrationError, match="Not enough"):
        calibration.finish(session, now=NOW + datetime.timedelta(seconds=400))


def test_finish_proposes_once_there_are_enough(reader, monkeypatch):
    session = _session(reader)
    monkeypatch.setattr(calibration, "_rows", lambda c, end: _uploads(values=[-50] * 13))

    calibration.finish(session, now=NOW + datetime.timedelta(seconds=400))

    assert session.status == CalibrationStatus.PROPOSED and session.result["can_approve"] is True


def test_limits_are_written_only_on_approval_and_can_be_cleared(reader, user):
    session = _session(reader)
    session.result = {"can_approve": True, "ports": {
        str(p): {"enter_rssi": -45, "leave_rssi": -65} for p in range(1, 5)}}
    session.status = CalibrationStatus.PROPOSED
    session.save()
    assert not Antenna.objects.filter(enter_rssi__isnull=False).exists()

    calibration.approve(session, user=user, now=NOW)

    assert Antenna.objects.filter(reader=reader, enter_rssi=-45, leave_rssi=-65).count() == 4
    assert session.check_until == NOW + datetime.timedelta(days=3)

    calibration.clear_limits(reader)
    assert not Antenna.objects.filter(enter_rssi__isnull=False).exists()


def test_the_calibration_screen_runs_a_session(client_in, parked):
    reader = parked["reader"]

    def act(action, **payload):
        return post(client_in, reverse("tracking-calibration", args=[reader.pk, action]), payload)

    assert act("start", vehicles=[parked["bikes"][0].pk]).json()["ok"] is True
    assert b"Keep the bikes parked" in client_in.get(reverse("tracking-antennas")).content
    status = client_in.get(reverse("tracking-calibration-status", args=[reader.pk])).json()
    assert status["ok"] is True and status["bikes"][0]["name"] == "MO 41" and status["ready"] is False
    assert act("finish").status_code == 400
    assert act("discard").json()["ok"] is True
    assert Calibration.objects.get().status == CalibrationStatus.DISCARDED


def test_a_session_left_open_expires(reader):
    session = _session(reader)
    session.started_at = timezone.now() - datetime.timedelta(hours=3)
    session.save()

    assert upkeep.hourly(timezone.now())["calibrations_expired"] == 1
    session.refresh_from_db()
    assert session.status == CalibrationStatus.DISCARDED


# -- the bell ---------------------------------------------------------------------------------

def event(world, **fields):
    values = {"company": world["company"], "branch": world["adc1"], "kind": EventKind.STATE,
              "at": timezone.now() - datetime.timedelta(minutes=1),
              "to_state": PresenceState.MISSING, "alerts": True}
    return PresenceEvent.objects.create(**{**values, **fields})


@pytest.fixture
def tracking_on(settings):
    settings.TRACKING_ENABLED = True


def test_the_bell_counts_unread_and_mark_read_clears_it(client_in, world, user, tracking_on):
    event(world)

    data = client_in.get(reverse("tracking-bell-feed")).json()
    assert (data["unread"], data["items"][0]["title"]) == (1, "Vehicle missing")

    assert post(client_in, reverse("tracking-bell-read"), {}).json()["ok"] is True
    assert client_in.get(reverse("tracking-bell-feed")).json()["unread"] == 0
    assert AlertRead.objects.filter(user=user).exists()


def test_the_bell_shows_only_alerts_of_the_users_own_company(client_in, world, tracking_on):
    event(world, alerts=False)
    event(world, company=world["other"], branch=world["theirs"])

    assert client_in.get(reverse("tracking-bell-feed")).json()["items"] == []


def test_a_user_without_the_privilege_gets_no_bell_feed(world, tracking_on):
    assert bell.bell_for(None) is None


def test_the_navbar_carries_the_feed_for_a_privileged_user(client_in, tracking_on):
    assert b"byky-tracking-bell" in client_in.get(reverse("tracking-status")).content


def test_with_tracking_off_there_is_no_bell_and_no_extra_query(client_in, user, django_assert_num_queries, world):
    assert b"byky-tracking-bell" not in client_in.get(reverse("tracking-status")).content
    with django_assert_num_queries(0):
        assert bell.bell_for(user) is None
    event(world)
    assert client_in.get(reverse("tracking-bell-feed")).json() == {"unread": 0, "items": []}


def test_a_warning_never_reaches_the_bell_screens(client_in):
    assert b"At edge" not in client_in.get(reverse("tracking-status")).content


def test_acknowledging_is_limited_to_the_users_companies(client_in, world):
    theirs = event(world, company=world["other"], branch=world["theirs"])

    assert post(client_in, reverse("tracking-alert-acknowledge", args=[theirs.pk]), {}).status_code == 404


def test_acknowledging_keeps_who_and_why(client_in, world, user):
    alert = event(world)

    post(client_in, reverse("tracking-alert-acknowledge", args=[alert.pk]), {"note": "Moved to the back"})

    alert.refresh_from_db()
    assert (alert.acknowledged_by, alert.note) == (user, "Moved to the back")


# -- upkeep -----------------------------------------------------------------------------------

def test_hourly_upkeep_makes_partitions_and_keeps_the_longest_retention(world):
    TrackingSettings.objects.create(company=world["company"], retention_days=7)
    TrackingSettings.objects.create(company=world["other"], retention_days=2)

    assert upkeep.retention_days() == 7
    assert upkeep.hourly(timezone.now()) is not None


def test_drift_beyond_6_db_warns_and_clears(world, reader, monkeypatch):
    antenna = Antenna.objects.create(reader=reader, port=1, baseline_rssi=-40)

    monkeypatch.setattr(upkeep, "parked_medians", lambda reader_id: {1: -50})
    assert upkeep.check_drift() == 1
    antenna.refresh_from_db()
    assert "Recalibrate" in antenna.drift_note

    monkeypatch.setattr(upkeep, "parked_medians", lambda reader_id: {1: -42})
    assert upkeep.check_drift() == 0
    antenna.refresh_from_db()
    assert antenna.drift_note == ""


# -- Raw Readings is for system administrators (owner, 9 Nov 2026) -------------------------------

def test_raw_readings_refuses_a_company_admin_and_leaves_it_out_of_the_sidebar(client_in):
    assert client_in.get(reverse("tracking-raw")).status_code == 403
    assert reverse("tracking-raw").encode() not in client_in.get(reverse("tracking-status")).content


@pytest.fixture
def system_admin(client, db):
    role = Role.objects.create(company=None, name="System Administrator")
    grant_all(role)
    User.objects.create_user("platform.admin", "Passw0rd!x", display_name="Platform", scope=UserScope.SYSTEM,
                             role=role, allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "platform.admin", "password": "Passw0rd!x"})
    return client


def _raw(reader, count, start):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO tracking_reading (reader_id, port, tag, reader_time, received_at, rssi, entry, staying, low_battery) "
            "SELECT %s, 1, '0A0B0C0D', NULL, %s + g * interval '1 second', -40, false, true, false "
            "FROM generate_series(1, %s) g", [reader.pk, start, count])


def test_raw_readings_opens_for_a_system_admin_and_says_when_nothing_is_kept(system_admin, reader):
    html = system_admin.get(reverse("tracking-raw")).content.decode()

    assert "Keep raw readings is off" in html


def test_raw_readings_pages_without_counting(system_admin, reader):
    _raw(reader, 150, timezone.now() - datetime.timedelta(minutes=20))

    first = system_admin.get(reverse("tracking-raw"), {"reader": reader.pk})
    second = system_admin.get(reverse("tracking-raw"), {"reader": reader.pk, "page": 2})

    assert len(first.context["rows"]) == 100 and first.context["next_query"]
    assert len(second.context["rows"]) == 50 and not second.context["next_query"] and second.context["prev_query"]


def test_a_slow_raw_search_is_told_not_crashed(system_admin, reader, monkeypatch):
    def slow(*args, **kwargs):
        raise OperationalError("canceling statement due to statement timeout")

    monkeypatch.setattr(views.Reading.objects, "filter", slow)

    assert b"took too long" in system_admin.get(reverse("tracking-raw"), {"reader": reader.pk}).content
