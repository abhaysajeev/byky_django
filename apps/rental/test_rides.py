"""Test rides: a station's tablet lets a customer or a staff member take one
vehicle out, free, for the station's test minutes (owner, 28 Oct 2026).

Ported from legacy Save_Test_Vehicles (start), Dismiss_Test_Vehicles (end)
and Save_Order_Booking:437-482 (a booking that names the test). Kept: one
test number from the tablet's own counter (BillType 3, the leading `T`); the
minutes are the station's Customer / Staff (legacy Cashier) test time slot;
a booking may name the test, which links the two. Not kept: several vehicles
per test (13 vehicles over 11 legacy tests -- one per test now); ID type,
ID number and email; a match by test number (legacy updated every row with
that number); the never-set IsCancelled -- a test is cancelled for real now.

While a vehicle is on a test ride it cannot be rented (apps/rental/
services.py::_line_refs) and its row in the vehicles call says so
(apps/fleet/services.py). Test minutes are never charged: a rental's own
start time drives its fare.
"""

import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.crew.models import Employee
from apps.devices import services as devices_services
from apps.devices.models import BillKind, DeviceSettings
from apps.fleet.models import Vehicle
from apps.rental import station_sync
from apps.rental.models import TestRide, TestRideMode, TestRideStatus
from core.enums import ApprovalStatus

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PAGE_SIZE = 50


class TestRideRefused(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _minutes(branch, mode):
    """The station's test minutes for this mode -- its approved device
    settings (Customer / Staff Test Time Slot)."""
    row = (DeviceSettings.objects.filter(branch=branch, is_active=True, approval_status=ApprovalStatus.APPROVED)
           .values("customer_test_minutes", "staff_test_minutes").first())
    if row is None:
        raise TestRideRefused("device_settings_not_done", "This station's device settings are not set up yet.")
    return row["customer_test_minutes"] if mode == TestRideMode.CUSTOMER else row["staff_test_minutes"]


def active_test_rides(vehicle_ids):
    """{vehicle_id: TestRide} for the vehicles in `vehicle_ids` on a test ride
    now (one per vehicle, enforced by the database)."""
    rides = TestRide.objects.filter(vehicle_id__in=list(vehicle_ids), status=TestRideStatus.ON_TEST)
    return {ride.vehicle_id: ride for ride in rides}


def start(session, values):
    """A new test ride from the tablet. Returns (ride, created): created is
    False when this sync_id was already stored (a resend)."""
    from apps.rental.services import active_rentals

    branch = session.branch
    company = branch.company
    sync_id = values["sync_id"]
    existing = TestRide.objects.filter(pk=sync_id).first()
    if existing is not None:
        if existing.company_id != company.pk:
            raise TestRideRefused("sync_id_conflict", "That sync_id is already used.")
        return existing, False
    if not branch.allows_test_ride:
        raise TestRideRefused("test_rides_not_allowed", "Test rides are not allowed at this station.")
    minutes = _minutes(branch, values["mode"])
    employee = None
    if values["mode"] == TestRideMode.STAFF:
        employee = Employee.objects.filter(pk=values["employee_id"], company=company, is_active=True,
                                           approval_status=ApprovalStatus.APPROVED).first()
        if employee is None:
            raise TestRideRefused("unknown_employee", "No active employee with that id.", 404)
        if employee.is_blocked:
            raise TestRideRefused("employee_blocked", "That employee is blocked.")
    with transaction.atomic():
        # Locked, so a booking of this vehicle and this test take turns.
        vehicle = Vehicle.objects.select_for_update().filter(pk=values["vehicle_id"], company=company).first()
        if vehicle is None:
            raise TestRideRefused("unknown_vehicle", "No vehicle with that id.", 404)
        if vehicle.current_branch_id != branch.pk:
            raise TestRideRefused("vehicle_not_at_station", "That vehicle is not at this station.", 400)
        if active_rentals([vehicle.pk]):
            raise TestRideRefused("vehicle_already_rented", "That vehicle is out on rent.")
        if active_test_rides([vehicle.pk]):
            raise TestRideRefused("vehicle_on_test_ride", "That vehicle is already on a test ride.")
        if TestRide.objects.filter(company=company, test_no=values["test_no"]).exists():
            raise TestRideRefused("test_no_used", "That test number is already used.")
        customer = values["mode"] == TestRideMode.CUSTOMER
        ride = TestRide(
            id=sync_id, company=company, branch=branch, vehicle=vehicle, test_no=values["test_no"],
            mode=values["mode"], customer_name=(values.get("customer_name") or "").strip() if customer else "",
            customer_mobile=(values.get("customer_mobile") or "").strip() if customer else "",
            employee=employee, allowed_minutes=minutes, started_at=values["started_at"],
            expected_end_at=values["started_at"] + datetime.timedelta(minutes=minutes),
            device=session.device, started_by=session.user, created_by=session.user, modified_by=session.user,
        )
        try:
            with transaction.atomic():
                ride.save(force_insert=True)
        except IntegrityError:
            raced = TestRide.objects.filter(pk=sync_id).first()
            if raced is not None:
                return raced, False
            raise TestRideRefused("vehicle_on_test_ride", "That vehicle is already on a test ride.") from None
        devices_services.raise_counter_from(session.device, branch, BillKind.TEST_RIDE, ride.test_no)
        station_sync.test_ride_changed(ride, "start", device=session.device)
    return ride, True


def _close(session, ride_id, status, at):
    """End or cancel a ride of this station. Returns (ride, changed): asking
    for the state it is already in changes nothing (a resend)."""
    with transaction.atomic():
        ride = TestRide.objects.select_for_update().filter(pk=ride_id, branch=session.branch).first()
        if ride is None:
            raise TestRideRefused("unknown_test_ride", "No test ride with that id at this station.", 404)
        if ride.status == status:
            return ride, False
        if ride.status != TestRideStatus.ON_TEST:
            raise TestRideRefused("test_ride_closed", f"This test ride is already {ride.get_status_display().lower()}.")
        ride.status, ride.ended_at, ride.ended_by, ride.modified_by = status, at, session.user, session.user
        ride.save(update_fields=["status", "ended_at", "ended_by", "modified_by", "modified_on"])
        station_sync.test_ride_changed(ride, "end" if status == TestRideStatus.ENDED else "cancel",
                                       device=session.device)
    return ride, True


def end(session, values):
    """The vehicle is back."""
    return _close(session, values["test_ride_id"], TestRideStatus.ENDED, values["ended_at"])


def cancel(session, values):
    """Started by mistake."""
    return _close(session, values["test_ride_id"], TestRideStatus.CANCELLED, values["cancelled_at"])


# -- A booking that names the test ride (apps/rental/services.py::_book) ----------------


def for_booking(session, ride_id):
    """The ride a booking names, locked -- this station's and still on test --
    or the refusal (an OrderRefused, as the booking answers)."""
    from apps.rental.services import OrderRefused

    ride = TestRide.objects.select_for_update().filter(pk=ride_id, branch=session.branch).first()
    if ride is None:
        raise OrderRefused("unknown_test_ride")
    if ride.status != TestRideStatus.ON_TEST:
        raise OrderRefused("test_ride_closed")
    return ride


def mark_rented(ride, order, session):
    """The booking went through: the test ride is closed as rented, linked to
    the order. Inside the booking's transaction -- a refused booking leaves the
    ride on test."""
    ride.status, ride.order, ride.ended_at = TestRideStatus.RENTED, order, order.start_time
    ride.ended_by, ride.modified_by = session.user, session.user
    ride.save(update_fields=["status", "order", "ended_at", "ended_by", "modified_by", "modified_on"])
    station_sync.test_ride_changed(ride, "rented", device=session.device)


# -- Reading -----------------------------------------------------------------------------


def _local(moment, zone):
    return moment.astimezone(zone).strftime(TIME_FORMAT) if moment else None


def is_overdue(ride, now=None):
    return ride.status == TestRideStatus.ON_TEST and (now or timezone.now()) > ride.expected_end_at


def ride_json(ride, zone, now=None):
    vehicle = ride.vehicle
    return {
        "test_ride_id": str(ride.pk), "test_no": ride.test_no, "mode": ride.mode,
        "vehicle_id": vehicle.pk, "vehicle_name": vehicle.vehicle_name, "vehicle_code": vehicle.vehicle_code,
        "customer_name": ride.customer_name, "customer_mobile": ride.customer_mobile,
        "employee_id": ride.employee_id,
        "employee_code": ride.employee.employee_code if ride.employee_id else None,
        "employee_name": ride.employee.full_name if ride.employee_id else None,
        "allowed_minutes": ride.allowed_minutes, "started_at": _local(ride.started_at, zone),
        "expected_end_at": _local(ride.expected_end_at, zone), "status": ride.status,
        "overdue": is_overdue(ride, now), "ended_at": _local(ride.ended_at, zone),
        "order_id": str(ride.order_id) if ride.order_id else None,
        "order_no": ride.order.order_no if ride.order_id else None,
        "started_by": _name(ride.started_by), "ended_by": _name(ride.ended_by),
        "branch_id": ride.branch_id, "station": ride.branch.name,
    }


def _name(user):
    if user is None:
        return None
    return user.display_name or user.username


def with_rows(rows):
    return rows.select_related("vehicle", "branch__location__state", "order", "employee", "started_by",
                               "ended_by")


def station_list(branch, *, open_only=True, status=None, mode=None, from_date=None, to_date=None, zone=None,
                 page=1):
    """This station's test rides: the ones on test now (the tablet's catch-up),
    or all of them, newest first."""
    from django.core.paginator import Paginator

    rows = TestRide.objects.filter(branch=branch)
    if open_only:
        rows = rows.filter(status=TestRideStatus.ON_TEST)
    elif status:
        rows = rows.filter(status=status)
    if mode:
        rows = rows.filter(mode=mode)
    rows = between(rows, zone, from_date, to_date)
    paged = Paginator(with_rows(rows).order_by("-started_at", "-pk"), PAGE_SIZE).get_page(page)
    return list(paged.object_list), paged


def between(rows, zone, from_date=None, to_date=None):
    if from_date:
        rows = rows.filter(started_at__gte=datetime.datetime.combine(from_date, datetime.time.min, tzinfo=zone))
    if to_date:
        rows = rows.filter(started_at__lt=datetime.datetime.combine(
            to_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=zone))
    return rows
