"""POST /api/v1/{app}/test-rides... -- operator app only (apps/rental/
test_rides.py). Start, end and cancel are parked like the other tablet writes
(apps/sync/parking.py); the list is the tablet's catch-up."""

from django.utils import timezone
from drf_spectacular.utils import extend_schema

from apps.devices import services as devices_services
from apps.devices.models import BillKind
from apps.rental import test_rides
from apps.rental.api import _COMMON, _OperatorView
from apps.rental.serializers import (
    TestRideCancelRequest,
    TestRideEndRequest,
    TestRideListRequest,
    TestRideStartRequest,
)
from apps.sync import parking
from apps.sync.models import IssueKind
from core.api import envelope, request_parts, station_of
from core.schema import envelope_request, envelope_responses
from core.timezones import zone_for

TAGS = ["Operator Test Rides"]

_RIDE_SAMPLE = {
    "test_ride_id": "01a1b5c6-0000-7000-8000-00000000000a", "test_no": "TADC160182000006", "mode": "customer",
    "vehicle_id": 412, "vehicle_name": "MO 41", "vehicle_code": "MO41",
    "customer_name": "Ahmed Al Mansoori", "customer_mobile": "971501234567",
    "employee_id": None, "employee_code": None, "employee_name": None,
    "allowed_minutes": 5, "started_at": "2026-10-28 11:10:00", "expected_end_at": "2026-10-28 11:15:00",
    "status": "on_test", "overdue": False, "ended_at": None, "order_id": None, "order_no": None,
    "started_by": "Ravi", "ended_by": None, "branch_id": 27, "station": "Abu Dhabi Corniche 1",
}
_STARTED_SAMPLE = {**_RIDE_SAMPLE, "next_test_number": 7}
_ENDED_SAMPLE = {**_RIDE_SAMPLE, "status": "ended", "ended_at": "2026-10-28 11:14:30", "ended_by": "Ravi"}

_START_DESCRIPTION = """
One vehicle out on a **free test ride** -- a customer (`mode: customer`, with
`customer_name` and `customer_mobile`) or a staff member (`mode: staff`, with
`employee_id` -- picked from `employees`, required even when the operator
rides). Only at a station that allows test rides.

* `sync_id` (UUIDv7) is this call's id and becomes the test ride's id; resent
  unchanged it answers `duplicate`.
* `test_no` is the printed test number: the tablet's test counter
  (`next_test_number` at login), with the leading `T`. Each reply carries the
  next one.
* The ride lasts the station's **Customer / Staff Test Time Slot**
  (device settings: `customer_test_minutes`, `staff_test_minutes`), copied at
  start: `allowed_minutes`, `expected_end_at`. `overdue` is true once it is
  past that and still on test.

While on test the vehicle **cannot be rented**: the `vehicles` call shows
`on_test_ride: true`, `can_rent: false`, and booking, add or replace with it
are refused `vehicle_on_test_ride`. To rent it after the test, **book with
`test_ride_id`** (`POST orders`): the test closes as `rented`, linked to the
order. Otherwise close it with `test-rides/end` (vehicle back) or
`test-rides/cancel` (started by mistake). Test minutes are never charged.

Refusals: `test_rides_not_allowed`, `unknown_employee` (not an active employee
of the company), `employee_blocked`, `unknown_vehicle`, `vehicle_not_at_station`,
`vehicle_already_rented` (out on rent), `vehicle_on_test_ride`, `test_no_used`,
`device_settings_not_done`, `sync_id_conflict`.

The station's other tablets get a quiet push (`type: test_ride_changed`) on
start, end, cancel and rent.
"""

_CLOSE_DESCRIPTION = """
Close a test ride of this station -- any tablet here. `end`: the vehicle is
back; `cancel`: it was started by mistake. Either frees the vehicle.

Sent again for a ride already in that state: `duplicate`. A ride already
closed another way (ended, rented or cancelled): `test_ride_closed`. Not this
station's: `unknown_test_ride`.
"""

_LIST_DESCRIPTION = """
This station's test rides, newest first. **`open`** (default `true`): only
those on test now -- call it at login, resume and reconnect to catch up with
the pushes missed. With `open: false`: every ride, optionally one `status`,
`mode` and a start-date range (`from_date`, `to_date`).
"""


def _refused(refusal):
    return envelope(refusal.code, refusal.message, http_status=refusal.status)


# The refusals, as apps/rental/test_rides.py answers them -- for the docs.
_MESSAGES = {
    "unknown_vehicle": "No vehicle with that id.",
    "vehicle_not_at_station": "That vehicle is not at this station.",
    "test_rides_not_allowed": "Test rides are not allowed at this station.",
    "vehicle_already_rented": "That vehicle is out on rent.",
    "vehicle_on_test_ride": "That vehicle is already on a test ride.",
    "test_no_used": "That test number is already used.",
    "device_settings_not_done": "This station's device settings are not set up yet.",
    "sync_id_conflict": "That sync_id is already used.",
    "unknown_test_ride": "No test ride with that id at this station.",
    "unknown_employee": "No active employee with that id.",
    "employee_blocked": "That employee is blocked.",
    "test_ride_closed": "This test ride is already ended.",
}


def _rows(*codes, status=409):
    return [(status, code, _MESSAGES[code], None) for code in codes]


def _next_test_number(session):
    return devices_services.counter_for(session.device, session.branch, BillKind.TEST_RIDE).next_number


class TestRideStartView(_OperatorView):
    """POST /api/v1/{app}/test-rides -- operator app only."""

    park_kind = IssueKind.TEST_RIDE_START

    @extend_schema(
        tags=TAGS, summary="Start a test ride (one vehicle)", description=_START_DESCRIPTION,
        request=envelope_request("TestRideStartEnvelope", TestRideStartRequest),
        responses=envelope_responses(
            (200, "ok", "Test ride started.", _STARTED_SAMPLE),
            (200, "duplicate", "Already started.", _STARTED_SAMPLE),
            (400, "invalid_request", "customer_name is required for a customer test ride.",
             {"errors": {"customer_name": "is required for a customer test ride"}}),
            *_rows("unknown_vehicle", "unknown_employee", status=404),
            *_rows("vehicle_not_at_station", status=400), *_rows("employee_blocked"),
            *_rows("test_rides_not_allowed", "vehicle_already_rented", "vehicle_on_test_ride", "test_no_used",
                   "device_settings_not_done", "sync_id_conflict"),
            *parking.SCHEMA_ROWS, *_COMMON,
        ),
    )
    def post(self, request, app):
        return self.parked(request, app)

    def perform(self, session, app, request_data, *, replay=False):
        branch, refused = station_of(session, replay=replay)
        if refused:
            return refused
        form = TestRideStartRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        zone = zone_for(branch.company)
        values["started_at"] = timezone.make_aware(values["started_at"], zone)
        try:
            ride, created = test_rides.start(session, values)
        except test_rides.TestRideRefused as refusal:
            return _refused(refusal)
        data = {**test_rides.ride_json(ride, zone), "next_test_number": _next_test_number(session)}
        return envelope("ok", "Test ride started.", data) if created else envelope("duplicate", "Already started.", data)


class _CloseView(_OperatorView):
    request_class = None
    at_field = ""
    done_message = ""

    def post(self, request, app):
        return self.parked(request, app)

    def close(self, session, values):
        raise NotImplementedError

    def perform(self, session, app, request_data, *, replay=False):
        branch, refused = station_of(session, replay=replay)
        if refused:
            return refused
        form = self.request_class(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        zone = zone_for(branch.company)
        values[self.at_field] = timezone.make_aware(values[self.at_field], zone)
        try:
            ride, changed = self.close(session, values)
        except test_rides.TestRideRefused as refusal:
            return _refused(refusal)
        data = test_rides.ride_json(ride, zone)
        return envelope("ok", self.done_message, data) if changed else envelope("duplicate", "Already recorded.", data)


_CLOSE_ROWS = (*_rows("unknown_test_ride", status=404), *_rows("test_ride_closed"))


class TestRideEndView(_CloseView):
    """POST /api/v1/{app}/test-rides/end -- operator app only."""

    park_kind = IssueKind.TEST_RIDE_END
    request_class, at_field, done_message = TestRideEndRequest, "ended_at", "Test ride ended."

    @extend_schema(
        tags=TAGS, summary="End a test ride (the vehicle is back)", description=_CLOSE_DESCRIPTION,
        request=envelope_request("TestRideEndEnvelope", TestRideEndRequest),
        responses=envelope_responses(
            (200, "ok", "Test ride ended.", _ENDED_SAMPLE), (200, "duplicate", "Already recorded.", _ENDED_SAMPLE),
            *_CLOSE_ROWS, *parking.SCHEMA_ROWS, *_COMMON,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def close(self, session, values):
        return test_rides.end(session, values)


class TestRideCancelView(_CloseView):
    """POST /api/v1/{app}/test-rides/cancel -- operator app only."""

    park_kind = IssueKind.TEST_RIDE_CANCEL
    request_class, at_field, done_message = TestRideCancelRequest, "cancelled_at", "Test ride cancelled."

    @extend_schema(
        tags=TAGS, summary="Cancel a test ride (started by mistake)", description=_CLOSE_DESCRIPTION,
        request=envelope_request("TestRideCancelEnvelope", TestRideCancelRequest),
        responses=envelope_responses(
            (200, "ok", "Test ride cancelled.", {**_ENDED_SAMPLE, "status": "cancelled"}),
            (200, "duplicate", "Already recorded.", {**_ENDED_SAMPLE, "status": "cancelled"}),
            *_CLOSE_ROWS, *parking.SCHEMA_ROWS, *_COMMON,
        ),
    )
    def post(self, request, app):
        return super().post(request, app)

    def close(self, session, values):
        return test_rides.cancel(session, values)


class TestRideListView(_OperatorView):
    """POST /api/v1/{app}/test-rides/list -- operator app only."""

    @extend_schema(
        tags=TAGS, summary="This station's test rides (open ones by default)", description=_LIST_DESCRIPTION,
        request=envelope_request("TestRideListEnvelope", TestRideListRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Test rides.", {"page": 1, "pages": 1, "total": 1, "test_rides": [_RIDE_SAMPLE]}),
            (400, "invalid_request", "status must be one of: on_test, ended, rented, cancelled.",
             {"errors": {"status": "must be one of: on_test, ended, rented, cancelled"}}),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        branch, refused = self.station(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = TestRideListRequest(data=request_data or {})
        form.is_valid(raise_exception=True)
        values = form.validated_data
        zone = zone_for(branch.company)
        rows, page = test_rides.station_list(
            branch, open_only=values["open"], status=values.get("status") or None, mode=values.get("mode") or None,
            from_date=values.get("from_date"), to_date=values.get("to_date"), zone=zone, page=values["page"])
        now = timezone.now()
        return envelope("ok", "Test rides.", {
            "page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count,
            "test_rides": [test_rides.ride_json(ride, zone, now) for ride in rows],
        })
