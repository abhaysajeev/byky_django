"""Observe calibration (owner, 18 Nov 2026 -- the old RMS way): no bikes parked
on the boundary; a past period's stored readings give each vehicle its own
range at the reader. The engine side is in test_engine.py."""

import datetime
import uuid

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.rental.models import TestRide, TestRideMode, TestRideStatus
from apps.tracking import calibration
from apps.tracking.models import (
    CalibrationMethod,
    CalibrationStatus,
    TrackingSettings,
    VehicleBaseline,
)
from apps.tracking.tests.test_screens import make_bike, parked, reader  # noqa: F401

pytestmark = pytest.mark.django_db


def stored(reader, tag, start, values, seconds=10, port=1):  # noqa: F811
    """One read every `seconds` from `start`, as the gateway stores them."""
    rows = [(reader.pk, port, tag, start + datetime.timedelta(seconds=n * seconds), rssi)
            for n, rssi in enumerate(values)]
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO tracking_reading (reader_id, port, tag, reader_time, received_at, rssi, entry, staying, "
            "low_battery) VALUES (%s, %s, %s, NULL, %s, %s, false, true, false)",
            [(r, p, t, at, rssi) for r, p, t, at, rssi in rows])


@pytest.fixture
def window():
    end = timezone.now().replace(microsecond=0) - datetime.timedelta(minutes=5)
    return end - datetime.timedelta(hours=1), end


def steady(level, count=60):
    """A parked bike's reads: around `level`, a few dB either way."""
    return [level + (-3, -1, 0, 1, 2)[n % 5] for n in range(count)]


# -- working out the ranges ---------------------------------------------------------------------

def test_each_vehicle_gets_its_own_range_from_stored_readings(parked, window):  # noqa: F811
    reader, (strong, weak) = parked["reader"], parked["bikes"]  # noqa: F811
    stored(reader, strong.rfid_epc, window[0], steady(-40))
    stored(reader, weak.rfid_epc, window[0], steady(-62))

    session = calibration.observe(reader, *window)

    assert (session.method, session.status) == (CalibrationMethod.OBSERVE, CalibrationStatus.PROPOSED)
    ranges = session.result["vehicles"]
    one, two = ranges[strong.rfid_epc], ranges[weak.rfid_epc]
    assert one["low_rssi"] <= -41 and one["high_rssi"] >= -39 and two["high_rssi"] <= -60
    assert one["mid_rssi"] == round((one["low_rssi"] + one["high_rssi"]) / 2) or abs(
        one["mid_rssi"] - (one["low_rssi"] + one["high_rssi"]) / 2) <= 1
    assert one["enter_rssi"] == one["low_rssi"] and one["leave_rssi"] == one["low_rssi"] - 3   # allowance 3 dB
    assert two["leave_rssi"] < one["leave_rssi"] - 15        # the weak tag is judged by its own normal
    assert session.result["can_approve"] and session.result["skipped"] == []
    assert not VehicleBaseline.objects.exists()               # nothing changes before approval


def test_a_bike_with_too_few_readings_is_skipped_and_named(parked, window):  # noqa: F811
    reader, (busy, quiet) = parked["reader"], parked["bikes"]  # noqa: F811
    stored(reader, busy.rfid_epc, window[0], steady(-45))
    stored(reader, quiet.rfid_epc, window[0], steady(-45, count=8))
    session = calibration.observe(reader, *window)
    assert list(session.result["vehicles"]) == [busy.rfid_epc]
    assert [s["name"] for s in session.result["skipped"]] == [quiet.vehicle_name]


def test_time_on_rent_or_a_test_ride_is_left_out(world, parked, window):  # noqa: F811
    reader, bike = parked["reader"], parked["bikes"][0]  # noqa: F811
    stored(reader, bike.rfid_epc, window[0], steady(-40))                                  # parked: first 10 min
    ride_from = window[0] + datetime.timedelta(minutes=15)
    stored(reader, bike.rfid_epc, ride_from, [-80] * 60)                                   # riding away
    TestRide.objects.create(id=uuid.uuid4(), company=world["company"], branch=world["adc1"], vehicle=bike,
                            test_no="T9", mode=TestRideMode.CUSTOMER, customer_name="Ali",
                            customer_mobile="971501234567", allowed_minutes=20, started_at=ride_from,
                            expected_end_at=ride_from + datetime.timedelta(minutes=20),
                            status=TestRideStatus.ENDED, ended_at=ride_from + datetime.timedelta(minutes=20))
    got = calibration.observe(reader, *window).result["vehicles"][bike.rfid_epc]
    assert got["low_rssi"] >= -45                            # the -80s of the ride never counted


@pytest.mark.parametrize(("shift", "message"), [
    (datetime.timedelta(days=5), "kept for 3 days"),          # beyond the retention
    (datetime.timedelta(minutes=50), "at least 30 minutes"),
])
def test_a_period_outside_the_rules_is_refused(parked, window, shift, message):  # noqa: F811
    start, end = window
    if "minutes" in message:
        start = end - datetime.timedelta(minutes=10)
    else:
        start = start - shift
    with pytest.raises(calibration.CalibrationError, match=message):
        calibration.observe(parked["reader"], start, end)


def test_a_period_in_the_future_and_capture_mode_are_refused(parked, window, settings):  # noqa: F811
    with pytest.raises(calibration.CalibrationError, match="in the past"):
        calibration.observe(parked["reader"], window[0], timezone.now() + datetime.timedelta(hours=1))
    settings.TRACKING_MODE = "capture"
    with pytest.raises(calibration.CalibrationError, match="capture"):
        calibration.observe(parked["reader"], *window)


def test_retention_follows_the_company_setting(parked, window):  # noqa: F811
    TrackingSettings.objects.update_or_create(company=parked["reader"].company, defaults={"retention_days": 7})
    start = window[0] - datetime.timedelta(days=5)
    session = calibration.observe(parked["reader"], start, window[1])           # within 7 days: allowed
    assert session.result["can_approve"] is False                                # no readings stored there


# -- approving, clearing -------------------------------------------------------------------------

def test_approve_fixes_the_ranges_and_replaces_the_old_ones(parked, window):  # noqa: F811
    reader, (one, two) = parked["reader"], parked["bikes"]  # noqa: F811
    stored(reader, one.rfid_epc, window[0], steady(-40))
    stored(reader, two.rfid_epc, window[0], steady(-55))
    first = calibration.observe(reader, *window)
    calibration.approve(first, user=None)
    assert {b.tag: b.vehicle_id for b in VehicleBaseline.objects.all()} == {one.rfid_epc: one.pk, two.rfid_epc: two.pk}
    assert first.check_until is not None

    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE tracking_reading")
    stored(reader, one.rfid_epc, window[0], steady(-48))
    second = calibration.observe(reader, *window)
    calibration.approve(second, user=None)
    [only] = VehicleBaseline.objects.all()
    assert (only.tag, only.calibration_id) == (one.rfid_epc, second.pk) and only.low_rssi <= -49


def test_clear_limits_clears_the_vehicle_ranges_too(parked, window):  # noqa: F811
    stored(parked["reader"], parked["bikes"][0].rfid_epc, window[0], steady(-40))
    calibration.approve(calibration.observe(parked["reader"], *window), user=None)
    calibration.clear_limits(parked["reader"])
    assert not VehicleBaseline.objects.exists()


# -- the screen -----------------------------------------------------------------------------------

def test_the_screen_offers_observe_and_shows_a_proposal(client_in, parked, window):  # noqa: F811
    url = reverse("tracking-antennas")
    html = client_in.get(url, {"reader": parked["reader"].pk}).content.decode()
    assert "Observe a past period" in html and 'name="window_from"' in html and "Or park bikes on the boundary" in html

    stored(parked["reader"], parked["bikes"][0].rfid_epc, window[0], steady(-40))
    zone = timezone.get_current_timezone()
    fmt = "%Y-%m-%dT%H:%M"
    reply = client_in.post(reverse("tracking-calibration", args=[parked["reader"].pk, "observe"]),
                           {"window_from": timezone.localtime(window[0], zone).strftime(fmt),
                            "window_to": timezone.localtime(window[1], zone).strftime(fmt)},
                           content_type="application/json")
    assert reply.json()["ok"] is True and "Review and approve" in reply.json()["message"]
    html = client_in.get(url, {"reader": parked["reader"].pk}).content.decode()
    assert "Approve ranges" in html and "MO 41" in html and "Missing below" in html
