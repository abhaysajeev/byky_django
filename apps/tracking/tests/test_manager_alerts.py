"""Tracking for the manager app (owner, 12 Nov 2026): tracking only in working
hours, the three manager APIs (apps/tracking/manager_api.py) and the pushes
(apps/tracking/alerts.py)."""

import datetime

import pytest

from apps.company.models import BranchWorkingTime
from apps.notifications import sender as sender_module
from apps.notifications.sender import PushError
from apps.portal.models import Page, Role, RolePermission
from apps.portal.session_models import AppSession
from apps.rental.tests import test_order_requests as rq
from apps.rental.tests.test_station_sync import FakeSender
from apps.tracking import alerts, engine, ops, tasks
from apps.tracking.models import EventKind, PresenceEvent, PresenceState, Reader
from apps.tracking.tests import test_engine as te
from core.enums import Channel

world, token, fresh_throttle, shop, manager = rq.world, rq.token, rq.fresh_throttle, rq.shop, rq.manager
station, at, tick, heard, alerts_on, on_test = te.station, te.at, te.tick, te.heard, te.alerts_on, te.on_test
call = rq.call
P = PresenceState
pytestmark = pytest.mark.django_db

SUMMARY = "/api/v1/manager/tracking/alerts/summary"
CURRENT = "/api/v1/manager/tracking/alerts/current"
HISTORY = "/api/v1/manager/tracking/alerts/history"
# te.T0 is Monday 9 Nov 2026, 08:00 UTC = 12:00 in Dubai
OPEN_NOW = (datetime.time(10, 0), datetime.time(20, 0))
SHUT_NOW = (datetime.time(0, 0), datetime.time(9, 0))


def hours(branch, start, end):
    BranchWorkingTime.objects.filter(branch=branch).delete()
    BranchWorkingTime.objects.create(branch=branch, week_day=0, shift_number=1, start_time=start, end_time=end)


def grant(world, read=True):
    role = Role.objects.get(company=world["company"], name="Operator")
    RolePermission.objects.update_or_create(role=role, page=Page.objects.get(code=alerts.PAGE),
                                            defaults={"can_read": read})


@pytest.fixture
def queued(monkeypatch):
    """What the engine hands the worker, instead of the worker."""
    found = []
    monkeypatch.setattr(tasks.manager_alert_push, "delay", lambda groups, at: found.extend(groups))
    return found


def cycle(station, seconds, capture, talking=True):
    with capture(execute=True):
        return tick(station, seconds, talking=talking)


def gone_missing(station, capture):
    """Heard at 0, missing at 300 (the default timings)."""
    tick(station, 0)
    heard(station, 0)
    tick(station, 30)
    return cycle(station, 300, capture)


# -- Working hours ---------------------------------------------------------------------


def test_an_overdue_test_ride_stays_on_test(world, station):
    on_test(world, station, 60)

    row = tick(station, 10_000)

    assert (row.state, row.reason) == (P.ON_TEST, "")
    assert not PresenceEvent.objects.filter(to_state=P.MISSING).exists()


def test_a_closed_station_raises_nothing(station):
    hours(station["branch"], *SHUT_NOW)
    alerts_on(station["branch"])
    tick(station, 0)
    heard(station, 0)

    row = tick(station, 10_000)
    Reader.objects.filter(pk=station["reader"].pk).update(last_heartbeat_at=None, last_data_at=None)
    tick(station, 10_100, talking=False)

    assert (row.state, row.reason) == (P.UNKNOWN, engine.STATION_CLOSED)
    assert not PresenceEvent.objects.exists()            # no missing, no reader offline


def test_opening_starts_the_clock_again(station):
    hours(station["branch"], *SHUT_NOW)
    tick(station, 0)
    heard(station, 0)
    assert tick(station, 5_000).reason == engine.STATION_CLOSED

    hours(station["branch"], *OPEN_NOW)
    assert tick(station, 5_030).state == P.PRESENT        # unheard all night, but the clock restarts
    assert tick(station, 5_329).state == P.PRESENT
    assert tick(station, 5_330).state == P.MISSING
    # closing and opening are not in the history; going missing is
    assert list(PresenceEvent.objects.values_list("to_state", flat=True)) == [P.MISSING]


def test_no_working_hours_is_tracked_all_day(station):
    tick(station, 0)
    heard(station, 0)
    tick(station, 30)

    assert tick(station, 300).state == P.MISSING


# -- Pushes ----------------------------------------------------------------------------


def test_missing_and_back_are_queued_per_station(station, queued, django_capture_on_commit_callbacks):
    alerts_on(station["branch"])
    gone_missing(station, django_capture_on_commit_callbacks)
    heard(station, 400)
    cycle(station, 400, django_capture_on_commit_callbacks)

    kinds = [(g["kind"], g["branch_id"], g["vehicle_ids"]) for g in queued]
    bike, branch = station["bike"].pk, station["branch"].pk
    assert kinds == [(alerts.MISSING, branch, [bike]), (alerts.BACK, branch, [bike])]


def test_nothing_is_queued_with_alerts_off_or_closed(station, queued, django_capture_on_commit_callbacks):
    gone_missing(station, django_capture_on_commit_callbacks)
    assert queued == []                                   # alerts off

    alerts_on(station["branch"])
    hours(station["branch"], *SHUT_NOW)
    Reader.objects.filter(pk=station["reader"].pk).update(last_heartbeat_at=None, last_data_at=None)
    cycle(station, 1_000, django_capture_on_commit_callbacks, talking=False)
    assert queued == []                                   # closed


def test_reader_offline_and_back_are_queued(station, queued, django_capture_on_commit_callbacks):
    alerts_on(station["branch"])
    tick(station, 0)                                      # the reader's first contact
    cycle(station, 301, django_capture_on_commit_callbacks, talking=False)
    cycle(station, 330, django_capture_on_commit_callbacks)

    assert [(g["kind"], g["reader_id"]) for g in queued] == [
        ("reader_offline", station["reader"].pk), ("reader_online", station["reader"].pk)]


def test_low_battery_is_never_pushed(station):
    event = PresenceEvent(pk=1, company_id=station["branch"].company_id, branch_id=station["branch"].pk,
                          vehicle_id=station["bike"].pk, kind=EventKind.LOW_BATTERY, at=at(0), alerts=True)

    assert alerts.groups([event], {station["branch"].pk}) == []


def test_missing_vehicles_are_one_push(station, shop):
    branch = station["branch"]
    events = [PresenceEvent(pk=n, company_id=branch.company_id, branch_id=branch.pk, vehicle_id=v.pk,
                            kind=EventKind.STATE, from_state=P.PRESENT, to_state=P.MISSING, at=at(0))
              for n, v in enumerate([shop["mo41"], shop["dc02"]], start=1)]

    found = alerts.groups(events, {branch.pk})

    assert len(found) == 1 and found[0]["event_ids"] == [1, 2]


@pytest.fixture
def phone(world, manager):
    AppSession.objects.filter(channel=Channel.MANAGER).update(push_token="tok-meera")
    return manager


@pytest.fixture
def fcm(monkeypatch):
    fake = FakeSender()
    monkeypatch.setattr(sender_module, "get_sender", lambda: fake)
    return fake


def group(station, kind=alerts.MISSING, **extra):
    return {"company_id": station["branch"].company_id, "branch_id": station["branch"].pk, "kind": kind,
            "vehicle_ids": [station["bike"].pk], "reader_id": None, "event_ids": [5], **extra}


def test_a_push_reaches_managers_who_may_read_alerts(world, station, phone, fcm):
    grant(world, read=False)
    assert alerts.push([group(station)], at(0)) == 0     # no Read on Missing & Alerts

    grant(world)
    assert alerts.push([group(station)], at(0).isoformat()) == 1
    token_, title, body, data, channel, tag, collapse = fcm.sent[0]
    branch = station["branch"]
    assert (token_, title, body) == ("tok-meera", f"{branch.name}: 1 vehicle missing", "MO 41")
    assert (channel, tag, collapse) == (alerts.SOUND, f"tracking:{branch.pk}:vehicles_missing",
                                        f"tracking:{branch.pk}:vehicles_missing")
    assert data == {"type": "tracking_alert", "kind": "vehicles_missing", "branch_id": str(branch.pk),
                    "vehicle_ids": str(station["bike"].pk), "reader_id": "", "event_ids": "5",
                    "at": "2026-11-09 12:00:00"}


def test_quiet_and_reader_pushes(world, station, phone, fcm):
    grant(world)
    reader = station["reader"].pk
    alerts.push([group(station, alerts.BACK), group(station, "reader_offline", vehicle_ids=[], reader_id=reader),
                 group(station, "reader_silent", vehicle_ids=[], reader_id=reader)], at(0))

    assert [(s[1].split(": ")[1], s[4]) for s in fcm.sent] == [
        ("1 vehicle back", alerts.QUIET), ("reader offline", alerts.SOUND), ("reader hears no tags", alerts.SOUND)]


def test_a_failed_send_is_only_logged(world, station, phone, fcm):
    grant(world)
    fcm.fail = PushError("unregistered")

    assert alerts.push([group(station)], at(0)) == 0
    assert AppSession.objects.get(channel=Channel.MANAGER).push_token == ""


# -- APIs ------------------------------------------------------------------------------


def get(client, manager, url, **request_data):
    body = call(client, url, request_data, token=manager).json()
    assert body["code"] == "ok", body
    return body["data"]


@pytest.fixture
def missing(world, station, manager, django_capture_on_commit_callbacks, queued):
    grant(world)
    gone_missing(station, django_capture_on_commit_callbacks)
    return station


def test_summary_counts_states_and_readers(client, world, missing, manager):
    data = get(client, manager, SUMMARY)

    assert data["totals"]["missing"] == 1
    row = data["stations"][0]
    assert (row["branch_id"], row["open"], row["working_hours_set"], row["hears_no_tags"]) == (
        missing["branch"].pk, True, False, False)
    assert row["counts"] == {"present": 0, "missing": 1, "unknown": 0, "on_rent": 0, "on_test": 0,
                             "in_service": 0, "low_battery": 0}
    assert [(r["reader_id"], r["online"]) for r in row["readers"]] == [(missing["reader"].pk, True)]


def test_summary_shows_a_closed_station(client, world, missing, manager):
    hours(missing["branch"], *SHUT_NOW)

    row = get(client, manager, SUMMARY)["stations"][0]

    assert (row["working_hours_set"], row["open"]) == (True, row["open"])   # open depends on the real clock


def test_current_lists_missing_and_filters(client, missing, manager):
    rows = get(client, manager, CURRENT)["vehicles"]
    assert [(r["vehicle_id"], r["state"], r["reason"]) for r in rows] == [(missing["bike"].pk, "missing", "not_seen")]

    assert get(client, manager, CURRENT, q="zzz")["vehicles"] == []
    assert get(client, manager, CURRENT, q="mo 4")["total"] == 1
    assert get(client, manager, CURRENT, low_battery=True)["total"] == 0
    assert get(client, manager, CURRENT, branch_id=999_999)["total"] == 0
    assert get(client, manager, CURRENT, state="all")["total"] == 1


def test_history_pages_events(client, missing, manager):
    data = get(client, manager, HISTORY, from_date="2026-11-09", to_date="2026-11-09")

    assert data["total_events"] == 2 and data["pages"] == 1          # newest first
    assert [(e["kind"], e["at"]) for e in data["events"]] == [
        ("vehicle_missing", "2026-11-09 12:05:00"), ("reader_online", "2026-11-09 12:00:00")]
    assert get(client, manager, HISTORY, from_date="2026-11-10", to_date="2026-11-10")["total_events"] == 0
    assert get(client, manager, HISTORY, from_date="2026-11-09", to_date="2026-11-09", page=3)["events"] == []


def test_history_dates_must_be_in_order(client, missing, manager):
    body = call(client, HISTORY, {"from_date": "2026-11-10", "to_date": "2026-11-09"}, token=manager).json()

    assert body["code"] == "invalid_request"


def test_without_read_on_alerts_it_is_refused(client, world, manager):
    grant(world, read=False)
    response = call(client, SUMMARY, {}, token=manager)

    assert response.status_code == 403 and response.json()["code"] == "forbidden"


def test_the_operator_app_is_refused(client, world, token):
    response = call(client, SUMMARY.replace("manager", "operator"), {}, token=token)

    assert response.json()["code"] == "wrong_channel"


# -- Logging ---------------------------------------------------------------------------


def test_tracking_events_are_not_shipped(monkeypatch, settings, caplog):
    settings.LOG_INGEST_URL, settings.LOG_INGEST_KEY = "http://logs.invalid/ingest", "k"
    caplog.set_level("INFO", logger="apps.tracking.ops")

    ops.event("reader_connected", reader="190619101016089")

    assert "reader_connected" in caplog.text
    assert not hasattr(ops, "_queue") and not hasattr(ops, "_ship")
