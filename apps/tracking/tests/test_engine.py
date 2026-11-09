"""The presence engine, on a frozen clock (design §5, §9.3)."""

import datetime
import uuid

import psycopg
import pytest
from django.db import connection

from apps.rental.models import TestRide, TestRideMode, TestRideStatus
from apps.rental.tests import test_order_api as orders
from apps.tracking import engine, store
from apps.tracking.models import (
    Antenna,
    BranchTrackingSettings,
    Calibration,
    CalibrationStatus,
    EventKind,
    PresenceEvent,
    PresenceState,
    Reader,
    ReaderAssignment,
    ReaderStatus,
    TagLastSeen,
    TrackingSettings,
    VehiclePresence,
)

world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop
pytestmark = pytest.mark.django_db

T0 = datetime.datetime(2026, 11, 9, 8, 0, tzinfo=datetime.UTC)
P = PresenceState
TAG = "0A0B0C0D"


def at(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


@pytest.fixture
def station(world, shop):
    reader = Reader.objects.create(device_id="190619101016089", company=world["company"], name="ADC1",
                                   status=ReaderStatus.APPROVED, enabled=True, last_heartbeat_at=T0)
    ReaderAssignment.objects.create(reader=reader, branch=world["adc1"], from_at=T0)
    bike = shop["mo41"]
    bike.rfid_epc = TAG
    bike.save(update_fields=["rfid_epc"])
    return {"reader": reader, "bike": bike, "branch": world["adc1"]}


def alerts_on(branch, **timings):
    BranchTrackingSettings.objects.update_or_create(branch=branch, defaults={"alerts_on": True, **timings})


def heartbeat(station, seconds):
    Reader.objects.filter(pk=station["reader"].pk).update(last_heartbeat_at=at(seconds))


def heard(station, seconds, rssi=-40, avg=None, port=1, low_battery=False, reader=None):
    TagLastSeen.objects.update_or_create(tag=TAG, defaults={
        "reader": reader or station["reader"], "port": port, "seen_at": at(seconds), "reader_time": at(seconds),
        "rssi": rssi, "rssi_avg": rssi if avg is None else avg, "low_battery": low_battery})


def tick(station, seconds, talking=True):
    if talking:
        heartbeat(station, seconds)
    assert engine.run_cycle(at(seconds)) is not None
    return VehiclePresence.objects.get(pk=station["bike"].pk)


def limits(station, enter=-50, leave=-60, port=1):
    Antenna.objects.update_or_create(reader=station["reader"], port=port,
                                     defaults={"enter_rssi": enter, "leave_rssi": leave})


def on_test(world, station, seconds_until_end):
    return TestRide.objects.create(
        id=uuid.uuid4(), company=world["company"], branch=station["branch"], vehicle=station["bike"], test_no="T1",
        mode=TestRideMode.CUSTOMER, customer_name="Ali", customer_mobile="971501234567", allowed_minutes=10, started_at=T0, status=TestRideStatus.ON_TEST,
        expected_end_at=at(seconds_until_end))


# -- every row of the table, first match wins ----------------------------------------------

def test_a_heard_bike_is_present(station):
    tick(station, 0)
    heard(station, 30)

    assert tick(station, 30).state == P.PRESENT


def test_rented_is_on_rent_even_when_unseen(station, monkeypatch):
    monkeypatch.setattr(engine, "active_rentals", lambda ids: {station["bike"].pk: ("A1", T0)})

    assert tick(station, 10_000).state == P.ON_RENT


def test_in_time_test_ride_is_excused(world, station):
    on_test(world, station, 600)

    assert tick(station, 500).state == P.ON_TEST


def test_overdue_test_ride_counts_as_missing(world, station):
    on_test(world, station, 60)

    row = tick(station, 61)

    assert (row.state, row.reason) == (P.MISSING, engine.TEST_OVERDUE)


def test_warehouse_service_is_excused(station, monkeypatch):
    monkeypatch.setattr(engine, "in_warehouse_service", lambda ids: {station["bike"].pk: "SRV1"})

    assert tick(station, 10_000).state == P.IN_SERVICE


def test_no_approved_reader_is_unknown(station):
    Reader.objects.update(status=ReaderStatus.PENDING)

    row = tick(station, 0)

    assert (row.state, row.reason) == (P.UNKNOWN, engine.NO_READER)


def test_a_switched_off_reader_is_unknown(station):
    Reader.objects.update(enabled=False)

    assert tick(station, 0).reason == engine.NO_READER


def test_reader_offline_is_unknown_not_missing(station):
    tick(station, 0)

    row = tick(station, 301, talking=False)

    assert (row.state, row.reason) == (P.UNKNOWN, engine.READER_OFFLINE)


def test_unseen_goes_straight_to_missing(station):
    tick(station, 0)
    heard(station, 0)
    tick(station, 30)

    assert tick(station, 299).state == P.PRESENT
    row = tick(station, 300)
    assert (row.state, row.reason) == (P.MISSING, engine.NOT_SEEN)


# -- the false alarms this engine must not raise --------------------------------------------

def test_a_4g_drop_alerts_nobody(station):
    """Alert (150 s) comes before offline (300 s): silence counts only while the
    reader talks."""
    alerts_on(station["branch"])
    tick(station, 0)
    heard(station, 0)
    tick(station, 30)

    assert tick(station, 200, talking=False).state == P.PRESENT
    assert tick(station, 299, talking=False).state == P.PRESENT
    assert tick(station, 331, talking=False).state == P.UNKNOWN
    assert not PresenceEvent.objects.filter(to_state=P.MISSING).exists()


def test_back_from_rent_is_not_flagged_for_having_been_away(station, monkeypatch):
    rented = {station["bike"].pk: ("A1", T0)}
    monkeypatch.setattr(engine, "active_rentals", lambda ids: dict(rented))
    tick(station, 0)
    limits(station)
    heard(station, 0, rssi=-80)              # the weak last read as it rode away
    tick(station, 3600)

    rented.clear()

    assert tick(station, 7200).state == P.PRESENT
    assert tick(station, 7499).state == P.PRESENT
    assert tick(station, 7500).state == P.MISSING  # and the clock does run from its return


def test_reader_back_online_restarts_the_clock(station):
    tick(station, 0)
    heard(station, 0)
    tick(station, 400, talking=False)        # unknown

    assert tick(station, 430).state == P.PRESENT


def test_a_replayed_seen_at_changes_nothing_while_absent(station):
    """Only the gateway moves seen_at (test_gateway_write); the engine reads it."""
    tick(station, 0)
    heard(station, 0)
    assert tick(station, 300).state == P.MISSING
    assert tick(station, 330).state == P.MISSING


# -- calibrated limits and hysteresis --------------------------------------------------------

def test_below_leave_shows_present_until_it_has_lasted_the_missing_time(station):
    tick(station, 0)
    limits(station)
    heard(station, 30, avg=-70)
    assert tick(station, 30).state == P.PRESENT

    heard(station, 200, avg=-70)
    assert tick(station, 200).state == P.PRESENT
    heard(station, 330, avg=-70)
    row = tick(station, 330)
    assert (row.state, row.reason) == (P.MISSING, engine.WEAK_SIGNAL)


def test_a_signal_that_recovers_resets_the_weak_timer(station):
    tick(station, 0)
    limits(station)
    heard(station, 30, avg=-70)
    tick(station, 30)
    heard(station, 200, avg=-55)             # back above leave
    assert tick(station, 200).weak_since is None
    heard(station, 330, avg=-70)
    assert tick(station, 330).state == P.PRESENT


def test_the_weak_timer_does_not_survive_a_rental(station, monkeypatch):
    rented = {}
    monkeypatch.setattr(engine, "active_rentals", lambda ids: dict(rented))
    tick(station, 0)
    limits(station)
    heard(station, 30, avg=-70)
    assert tick(station, 30).weak_since == at(30)
    rented[station["bike"].pk] = ("A1", T0)
    assert tick(station, 60).weak_since is None
    rented.clear()
    heard(station, 700, avg=-70)
    tick(station, 690)

    assert tick(station, 700).state == P.PRESENT


def test_back_to_present_needs_enter_not_just_leave(station):
    tick(station, 0)
    limits(station)
    heard(station, 30, avg=-70)
    tick(station, 30)
    heard(station, 330, avg=-70)
    assert tick(station, 330).state == P.MISSING

    heard(station, 360, avg=-55)             # between leave and enter
    row = tick(station, 360)
    assert (row.state, row.reason) == (P.MISSING, engine.WEAK_SIGNAL)
    heard(station, 390, avg=-45)
    assert tick(station, 390).state == P.PRESENT


def test_no_flapping_on_reads_around_the_band(station):
    tick(station, 0)
    limits(station)
    heard(station, 30, avg=-45)
    assert tick(station, 30).state == P.PRESENT
    before = PresenceEvent.objects.count()

    for n, avg in enumerate([-55, -49, -58, -52, -59]):
        seconds = 60 + n * 30
        heard(station, seconds, avg=avg)
        assert tick(station, seconds).state == P.PRESENT

    assert PresenceEvent.objects.count() == before


def test_with_no_limits_any_new_read_brings_it_back(station):
    tick(station, 0)
    heard(station, 0)
    assert tick(station, 300).state == P.MISSING

    heard(station, 310, avg=-90)

    assert tick(station, 310).state == P.PRESENT


# -- heard elsewhere, tags, battery ----------------------------------------------------------

def test_heard_at_another_station_stays_missing_and_says_where(world, station):
    other = Reader.objects.create(device_id="190619101016097", company=world["company"], status=ReaderStatus.APPROVED,
                                  enabled=True, last_heartbeat_at=T0)
    ReaderAssignment.objects.create(reader=other, branch=world["adc2"], from_at=T0)
    tick(station, 0)
    heard(station, 0)
    tick(station, 30)
    heard(station, 200, reader=other)

    row = tick(station, 300)

    assert (row.state, row.heard_by_branch, row.branch) == (P.MISSING, world["adc2"], station["branch"])
    assert row.last_seen_at == at(0)         # its own station's last hearing is kept


def test_a_tag_that_lost_leading_zeros_still_matches(station):
    station["bike"].rfid_epc = "a0b0c0d"
    station["bike"].save(update_fields=["rfid_epc"])
    tick(station, 0)
    heard(station, 30)                       # TAG = "0A0B0C0D"

    assert tick(station, 30).last_seen_at == at(30)


def test_low_battery_is_a_flag_and_one_event(station):
    alerts_on(station["branch"])
    tick(station, 0)
    heard(station, 30, low_battery=True)
    row = tick(station, 30)
    assert (row.state, row.low_battery) == (P.PRESENT, True)

    heard(station, 60, low_battery=True)
    tick(station, 60)

    event = PresenceEvent.objects.get(kind=EventKind.LOW_BATTERY)
    assert event.alerts is True


# -- events, alerts per branch ---------------------------------------------------------------

def test_events_only_on_change_and_alerts_only_with_alerts_on(station):
    tick(station, 0)
    heard(station, 0)
    tick(station, 30)
    tick(station, 300)
    tick(station, 330)

    event = PresenceEvent.objects.get(to_state=P.MISSING)

    assert (event.from_state, event.alerts) == (P.PRESENT, False)
    assert PresenceEvent.objects.filter(kind=EventKind.STATE).count() == 1


def test_a_branch_with_alerts_on_rings_the_bell(station):
    alerts_on(station["branch"])
    tick(station, 0)
    heard(station, 0)
    tick(station, 300)

    assert PresenceEvent.objects.get(to_state=P.MISSING).alerts is True


def test_reader_offline_and_back_are_events(station):
    alerts_on(station["branch"])
    tick(station, 0)
    tick(station, 301, talking=False)
    tick(station, 330)

    kinds = list(PresenceEvent.objects.exclude(kind=EventKind.STATE).order_by("at").values_list("kind", flat=True))
    assert kinds == [EventKind.READER_ONLINE, EventKind.READER_OFFLINE, EventKind.READER_ONLINE]


# -- settings: company default, per-branch overrides ------------------------------------------

def test_the_company_default_applies(world, station):
    TrackingSettings.objects.create(company=world["company"], missing_after_s=120)
    tick(station, 0)
    heard(station, 0)

    assert tick(station, 119).state == P.PRESENT
    assert tick(station, 120).state == P.MISSING


def test_a_branch_override_beats_the_company_default(world, station):
    TrackingSettings.objects.create(company=world["company"], missing_after_s=120)
    BranchTrackingSettings.objects.create(branch=station["branch"], missing_after_s=600)
    tick(station, 0)
    heard(station, 0)

    assert tick(station, 300).state == P.PRESENT
    assert tick(station, 600).state == P.MISSING


def test_blank_override_falls_back_field_by_field(world, station):
    TrackingSettings.objects.create(company=world["company"], missing_after_s=120, offline_after_s=240)
    BranchTrackingSettings.objects.create(branch=station["branch"], missing_after_s=600)

    timing = engine.timings_by_branch([station["branch"].pk])[station["branch"].pk]

    assert (timing.missing, timing.offline) == (600, 240)


def test_a_settings_change_applies_on_the_next_cycle(station):
    tick(station, 0)
    heard(station, 0)
    assert tick(station, 60).state == P.PRESENT

    BranchTrackingSettings.objects.create(branch=station["branch"], missing_after_s=90)

    assert tick(station, 90).state == P.MISSING


# -- the rows the engine keeps ----------------------------------------------------------------

def test_a_vehicle_that_loses_its_tag_leaves_the_board(station):
    tick(station, 0)
    station["bike"].rfid_epc = ""
    station["bike"].save(update_fields=["rfid_epc"])

    engine.run_cycle(at(30))

    assert not VehiclePresence.objects.exists()


def test_a_move_to_another_station_restarts_the_clock(world, station):
    other = Reader.objects.create(device_id="190619101016097", company=world["company"], status=ReaderStatus.APPROVED,
                                  enabled=True, last_heartbeat_at=at(300))
    ReaderAssignment.objects.create(reader=other, branch=world["adc2"], from_at=T0)
    tick(station, 0)
    station["bike"].current_branch = world["adc2"]
    station["bike"].save(update_fields=["current_branch"])

    row = tick(station, 300)

    assert (row.state, row.branch) == (P.PRESENT, world["adc2"])


# -- it must not hurt: lock and skip -----------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_a_second_engine_skips(station):
    with psycopg.connect(store.dsn(), autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", [engine.LOCK_KEY])
        heartbeat(station, 0)

        assert engine.run_cycle(at(0)) is None
        assert not VehiclePresence.objects.exists()


def test_a_failed_read_skips_the_cycle_and_flags_nobody(station, monkeypatch):
    tick(station, 0)
    heard(station, 0)
    tick(station, 30)
    alerts_before = PresenceEvent.objects.count()
    heartbeat(station, 1000)

    def down(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(TagLastSeen.objects, "filter", down)
    monkeypatch.setattr(connection, "close", lambda: None)   # the test's own transaction lives on it

    assert engine.tick(at(1000)) is None
    assert VehiclePresence.objects.get().state == P.PRESENT
    assert PresenceEvent.objects.count() == alerts_before


# -- a talking reader that hears nothing ----------------------------------------------------

@pytest.fixture
def fleet(station, shop):
    """Three tagged bikes at the station."""
    bikes = [station["bike"], shop["dc02"], shop["mo42"]]
    for bike, tag in zip(bikes[1:], ("0A0B0C0E", "0A0B0C0F"), strict=True):
        bike.rfid_epc = tag
        bike.save(update_fields=["rfid_epc"])
    return {**station, "bikes": bikes, "tags": [TAG, "0A0B0C0E", "0A0B0C0F"]}


def hear(fleet, seconds, tags=None):
    for tag in tags if tags is not None else fleet["tags"]:
        TagLastSeen.objects.update_or_create(tag=tag, defaults={
            "reader": fleet["reader"], "port": 1, "seen_at": at(seconds), "reader_time": at(seconds), "rssi": -40,
            "rssi_avg": -40.0})


def states(fleet):
    return [VehiclePresence.objects.get(pk=b.pk).state for b in fleet["bikes"]]


def settled(fleet):
    """A working station: every bike heard, up to 300 s."""
    tick(fleet, 0)
    for seconds in (60, 120, 180, 240, 300):
        hear(fleet, seconds)
        tick(fleet, seconds)
    assert states(fleet) == [P.PRESENT] * 3


def test_a_reader_that_hears_nothing_flags_nobody_and_alerts_once(fleet):
    alerts_on(fleet["branch"])
    settled(fleet)

    for seconds in (540, 570, 700, 900, 1200):
        tick(fleet, seconds)
        assert states(fleet) == [P.UNKNOWN] * 3

    assert VehiclePresence.objects.filter(reason=engine.NO_NEW_READS).count() == 3
    assert PresenceEvent.objects.filter(kind=EventKind.READER_SILENT, alerts=True).count() == 1
    assert not PresenceEvent.objects.filter(to_state=P.MISSING).exists()


def test_the_silence_guard_does_not_flap(fleet):
    alerts_on(fleet["branch"])
    settled(fleet)
    for seconds in range(540, 2400, 30):
        tick(fleet, seconds)

    changes = PresenceEvent.objects.filter(kind=EventKind.STATE).count()
    assert changes == 3                      # present -> unknown, once each
    assert PresenceEvent.objects.filter(kind=EventKind.READER_SILENT).count() == 1


def test_one_stolen_bike_at_a_working_station_still_goes_missing(fleet):
    settled(fleet)
    for seconds in range(360, 660, 30):
        hear(fleet, seconds, fleet["tags"][1:])
        tick(fleet, seconds)

    assert states(fleet) == [P.MISSING, P.PRESENT, P.PRESENT]


def test_a_reader_back_from_a_drop_raises_no_silence_alert(fleet):
    settled(fleet)
    tick(fleet, 700, talking=False)          # offline -> unknown
    tick(fleet, 730)                         # talking again, its first upload not here yet

    assert not PresenceEvent.objects.filter(kind=EventKind.READER_SILENT).exists()


def test_when_the_antennas_recover_a_bike_that_really_left_goes_missing(fleet):
    settled(fleet)
    tick(fleet, 570)
    assert states(fleet) == [P.UNKNOWN] * 3

    hear(fleet, 600, fleet["tags"][1:])
    tick(fleet, 600)
    assert states(fleet)[1:] == [P.PRESENT, P.PRESENT]

    for seconds in range(630, 960, 30):
        hear(fleet, seconds, fleet["tags"][1:])
        tick(fleet, seconds)
    assert states(fleet)[0] == P.MISSING


# -- calibration, and what the engine writes ----------------------------------------------

def test_a_bike_in_an_open_calibration_is_excused_and_starts_afresh(station):
    tick(station, 0)
    heard(station, 0)
    tick(station, 30)
    session = Calibration.objects.create(reader=station["reader"], tags=[TAG], started_at=at(40))

    row = tick(station, 400)
    assert (row.state, row.reason) == (P.UNKNOWN, engine.CALIBRATING)

    session.status = CalibrationStatus.DISCARDED
    session.save(update_fields=["status"])
    assert tick(station, 430).state == P.PRESENT


def test_a_proposed_session_no_longer_excuses_a_bike(station):
    tick(station, 0)
    heard(station, 0)
    tick(station, 30)
    Calibration.objects.create(reader=station["reader"], tags=[TAG], started_at=at(40),
                               status=CalibrationStatus.PROPOSED)

    assert tick(station, 300).state == P.MISSING


def test_nothing_is_written_or_published_when_nothing_changed(station, monkeypatch):
    sent = []
    monkeypatch.setattr(engine, "publish", lambda group, message: sent.append(group))
    tick(station, 0)
    heard(station, 30)
    tick(station, 30)
    sent.clear()

    heard(station, 60)
    heartbeat(station, 60)
    result = engine.run_cycle(at(60))

    assert (result["written"], sent) == (0, [])
