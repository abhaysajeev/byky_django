"""The Tracking screens, settings, readers, calibration and the bell."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.company.models import Branch, BranchType
from apps.tracking import bell, calibration, services, upkeep
from apps.tracking.models import (
    AlertRead,
    Antenna,
    BranchTrackingSettings,
    Calibration,
    CalibrationMethod,
    CalibrationStatus,
    EventKind,
    PointZone,
    PresenceEvent,
    PresenceState,
    Reader,
    ReaderAssignment,
    ReaderStatus,
    TrackingSettings,
)

pytestmark = pytest.mark.django_db

SCREENS = ["tracking-status", "tracking-alerts", "tracking-readers", "tracking-antennas", "tracking-raw",
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
    values = {"alert_after_s": 150, "missing_after_s": 300, "offline_after_s": 300, **default}
    return {"retention_days": 3, "default": values, "branches": branch or []}


def save(client, world, payload):
    return post(client, reverse("tracking-settings-save", args=[world["company"].pk]), payload)


def test_saves_company_default_and_per_branch_overrides(client_in, world):
    response = save(client_in, world, body([
        {"branch": world["adc1"].pk, "alerts_on": True, "alert_after_s": "60", "missing_after_s": "120",
         "offline_after_s": ""}], alert_after_s=100))

    assert response.json()["ok"] is True
    assert TrackingSettings.objects.get(company=world["company"]).alert_after_s == 100
    row = BranchTrackingSettings.objects.get(branch=world["adc1"])
    assert (row.alerts_on, row.alert_after_s, row.missing_after_s, row.offline_after_s) == (True, 60, 120, None)


@pytest.mark.parametrize("default,branch,field", [
    ({"alert_after_s": 300, "missing_after_s": 300}, {}, "Company default"),
    ({"alert_after_s": 20}, {}, "Company default: Alert after"),
    ({}, {"alert_after_s": "400"}, "Abu Dhabi Corniche 1"),
    ({}, {"missing_after_s": "9999"}, "Abu Dhabi Corniche 1: Missing after"),
], ids=["alert-not-before-missing", "too-short", "override-crosses-the-default", "too-long"])
def test_refused_settings_save_nothing(client_in, world, default, branch, field):
    response = save(client_in, world, body([{"branch": world["adc1"].pk, **branch}], **default))

    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == field
    assert not TrackingSettings.objects.exists() and not BranchTrackingSettings.objects.exists()


def test_a_default_that_breaks_an_override_is_refused(client_in, world):
    BranchTrackingSettings.objects.create(branch=world["adc1"], alert_after_s=200)

    response = save(client_in, world, body([{"branch": world["adc1"].pk, "alert_after_s": "200"}],
                                           alert_after_s=60, missing_after_s=150))

    assert response.status_code == 400


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


def test_a_reader_can_be_added_in_advance_and_not_twice(world):
    services.add_reader("190619101016097", company=world["company"])

    with pytest.raises(services.TrackingError):
        services.add_reader("190619101016097", company=world["company"])
    assert Reader.objects.get().status == ReaderStatus.PENDING


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

def _session(reader, method=CalibrationMethod.FULL):
    return calibration.start(reader, method, "170732EA", now=NOW)


def _sample(*groups):
    """[(zone, label, {port: rssi list})] as the points of a survey."""
    return [(calibration.CalibrationPoint(seq=n, label=label, zone=zone), by_port)
            for n, (zone, label, by_port) in enumerate(groups, 1)]


def test_full_survey_proposes_p10_inside_and_p90_outside_plus_margin(reader):
    samples = _sample((PointZone.INSIDE, "Middle", {1: [-40] * 9 + [-50]}),
                      (PointZone.OUTSIDE, "Road", {1: [-70] * 10}))

    proposal = calibration.propose(_session(reader), samples)

    assert proposal["ports"]["1"]["enter_rssi"] == -41
    assert proposal["ports"]["1"]["leave_rssi"] == -68


def test_overlap_proposes_nothing_and_says_why(reader):
    samples = _sample((PointZone.INSIDE, "Far corner", {1: [-70] * 10}),
                      (PointZone.OUTSIDE, "Gate", {1: [-40] * 10}))

    proposal = calibration.propose(_session(reader), samples)

    assert proposal["can_approve"] is False
    assert proposal["ports"]["1"]["enter_rssi"] is None and "overlap" in proposal["ports"]["1"]["note"]


def test_quick_method_uses_the_boundary_and_allowances(reader):
    TrackingSettings.objects.create(company=reader.company)
    samples = _sample((PointZone.BOUNDARY, "Side", {1: [-50] * 10}), (PointZone.OUTSIDE, "Road", {1: [-75] * 4}))

    port = calibration.propose(_session(reader, CalibrationMethod.QUICK), samples)["ports"]["1"]

    assert (port["enter_rssi"], port["leave_rssi"]) == (-47, -53)


def test_a_point_counts_for_its_strongest_antenna_and_others_take_the_pooled_limits(reader):
    samples = _sample((PointZone.INSIDE, "A", {1: [-40] * 5, 2: [-70] * 5}),
                      (PointZone.OUTSIDE, "B", {1: [-75] * 5, 2: [-80] * 5}))

    ports = calibration.propose(_session(reader), samples)["ports"]

    assert ports["1"]["source"] == "own" and ports["2"]["source"] == "pooled"
    assert ports["2"]["enter_rssi"] == ports["1"]["enter_rssi"]


def test_limits_are_written_only_on_approval(reader, user):
    session = _session(reader)
    session.result = {"can_approve": True, "ports": {
        str(p): {"enter_rssi": -45, "leave_rssi": -65} for p in range(1, 5)}}
    session.status = CalibrationStatus.PROPOSED
    session.save()
    assert not Antenna.objects.filter(enter_rssi__isnull=False).exists()

    calibration.approve(session, user=user, now=NOW)

    assert Antenna.objects.filter(reader=reader, enter_rssi=-45, leave_rssi=-65).count() == 4
    assert session.check_until == NOW + datetime.timedelta(days=3)


def test_a_proposal_with_no_limits_cannot_be_approved(reader, world):
    session = _session(reader)
    session.result, session.status = {"can_approve": False, "ports": {}}, CalibrationStatus.PROPOSED
    session.save()

    with pytest.raises(calibration.CalibrationError):
        calibration.approve(session, user=None)


def test_one_session_at_a_time_per_reader(reader):
    _session(reader)

    with pytest.raises(calibration.CalibrationError):
        _session(reader)


def test_bad_tags_are_refused():
    with pytest.raises(calibration.CalibrationError):
        calibration.parse_tags("NOTAHEXTAG")


def test_tags_are_padded_and_upper_cased():
    assert calibration.parse_tags("a0b0c0d, 170732EA a0b0c0d") == ["0A0B0C0D", "170732EA"]


def test_points_label_the_readings_between_them(reader):
    session = _session(reader)

    first = calibration.next_point(session, "Corner NE", PointZone.BOUNDARY, now=NOW)
    second = calibration.next_point(session, "Gate", PointZone.OUTSIDE, now=NOW + datetime.timedelta(minutes=2))

    first.refresh_from_db()
    assert first.ended_at == second.started_at and second.seq == 2


def test_the_calibration_screen_runs_a_session(client_in, world, reader):
    def act(action, **payload):
        return post(client_in, reverse("tracking-calibration", args=[reader.pk, action]), payload)

    assert act("start", method="full", tags="170732EA").json()["ok"] is True
    assert act("point", label="Middle", zone="inside").json()["ok"] is True
    assert client_in.get(reverse("tracking-antennas")).status_code == 200
    assert act("finish").json()["ok"] is True
    assert b"Proposed limits" in client_in.get(reverse("tracking-antennas")).content
    assert act("discard").json()["ok"] is True
    assert Calibration.objects.get().status == CalibrationStatus.DISCARDED


# -- the bell ---------------------------------------------------------------------------------

def event(world, **fields):
    values = {"company": world["company"], "branch": world["adc1"], "kind": EventKind.STATE,
              "at": timezone.now() - datetime.timedelta(minutes=1),
              "to_state": PresenceState.MISSING, "alerts": True}
    return PresenceEvent.objects.create(**{**values, **fields})


def test_the_bell_counts_unread_and_mark_read_clears_it(client_in, world, user):
    event(world)

    data = client_in.get(reverse("tracking-bell-feed")).json()
    assert (data["unread"], data["items"][0]["title"]) == (1, "Vehicle missing")

    assert post(client_in, reverse("tracking-bell-read"), {}).json()["ok"] is True
    assert client_in.get(reverse("tracking-bell-feed")).json()["unread"] == 0
    assert AlertRead.objects.filter(user=user).exists()


def test_the_bell_shows_only_alerts_of_the_users_own_company(client_in, world):
    event(world, alerts=False)
    event(world, company=world["other"], branch=world["theirs"])

    assert client_in.get(reverse("tracking-bell-feed")).json()["items"] == []


def test_a_user_without_the_privilege_gets_no_bell_feed(world):
    assert bell.bell_for(None) is None


def test_the_navbar_carries_the_feed_for_a_privileged_user(client_in):
    assert b"byky-tracking-bell" in client_in.get(reverse("tracking-status")).content


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
