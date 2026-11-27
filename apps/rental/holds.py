"""Cart holds: a vehicle in one operator tablet's cart at a multi-device station.

Owner, 18 Nov 2026 (no legacy counterpart -- the legacy had no cart):

- Only where the branch master says the station is multi-device
  (Branch.is_multi_device, sent to the tablet at login). Elsewhere the cart
  calls answer `single_device_branch` and holds are never consulted.
- While a hold is live, no other tablet at the station can cart the vehicle,
  and booking, add and replace with it are refused `vehicle_in_other_cart`.
- A hold lasts 10 minutes from the tablet's last cart call: every cart call
  restarts the clock on all of that tablet's holds. Server time decides.
- Operator app only. Nothing on the web.

A hold is live while it is not released, not expired, and the session that
made it is still signed in (not logged out, refresh token not expired --
apps/devices/services.py::live_sessions). So every way a tablet is signed
out -- logout, force logout, a newer sign-in, a password change, blocking,
re-mapping -- frees its holds with no hook of its own. A hold that stopped
being live is released lazily: by the next `add` of that vehicle (under its
row lock) and by the beat sweep `release_stale`, so the one-open-hold-per-
vehicle index stays honest.

A booking replayed from a parked call (apps/sync) is never refused for a
hold: the bike already left with the customer. It releases the other
tablet's hold instead.
"""

import datetime
import logging

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.rental.models import HoldRelease, VehicleHold

log = logging.getLogger(__name__)

TTL_MINUTES = 10
TTL = datetime.timedelta(minutes=TTL_MINUTES)
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class HoldRefused(Exception):
    """A cart call refused: code, HTTP status, message and the reply's data."""

    def __init__(self, code, status, message, data=None):
        self.code, self.status, self.message, self.data = code, status, message, data or {}
        super().__init__(message)


MESSAGES = {
    "single_device_branch": (409, "The cart is only used at multi-device stations."),
    "vehicle_in_other_cart": (409, "That vehicle is in another tablet's cart."),
    "unknown_vehicle": (404, "No vehicle with that id."),
    "vehicle_not_at_station": (400, "That vehicle is not at this station."),
    "vehicle_already_rented": (409, "That vehicle is out on rent."),
    "vehicle_on_test_ride": (409, "That vehicle is out on a test ride."),
    "vehicle_in_warehouse_service": (409, "That vehicle is in warehouse service."),
    "sync_id_conflict": (409, "That sync_id is already used for another vehicle or tablet."),
}


def refuse(code, data=None):
    status, message = MESSAGES[code]
    return HoldRefused(code, status, message, data)


# -- What is live ---------------------------------------------------------------------


def _live_q(now):
    return Q(released_at__isnull=True, expires_at__gt=now, branch__is_multi_device=True,
             session__logged_out_at__isnull=True, session__refresh_expires_at__gt=now)


def live(now=None):
    """Every live hold."""
    return VehicleHold.objects.filter(_live_q(now or timezone.now()))


def _stale_reason(hold, now):
    """Why an open hold is no longer live, or None if it is."""
    session = hold.session
    if session.logged_out_at is not None or session.refresh_expires_at is None \
            or session.refresh_expires_at <= now:
        return HoldRelease.LOGOUT
    if hold.expires_at <= now or not hold.branch.is_multi_device:
        return HoldRelease.EXPIRED
    return None


def _release(holds, reason, now):
    """Close these open holds. Returns how many."""
    return VehicleHold.objects.filter(pk__in=[h.pk for h in holds], released_at__isnull=True).update(
        released_at=now, release_reason=reason, modified_on=now,
    )


def release_stale(now=None):
    """The beat sweep: release every open hold that stopped being live.
    Returns {reason: count}."""
    now = now or timezone.now()
    done = {}
    open_holds = VehicleHold.objects.filter(released_at__isnull=True).exclude(_live_q(now))
    for reason, q in (
        (HoldRelease.LOGOUT, Q(session__logged_out_at__isnull=False) | Q(session__refresh_expires_at__isnull=True)
         | Q(session__refresh_expires_at__lte=now)),
        (HoldRelease.EXPIRED, Q()),
    ):
        n = open_holds.filter(q, released_at__isnull=True).update(
            released_at=now, release_reason=reason, modified_on=now)
        if n:
            done[reason] = n
    if done:
        log.info("released stale cart holds: %s", done)
    return done


# -- Reply shapes -----------------------------------------------------------------------


def _local(value, zone):
    return timezone.localtime(value, zone).strftime(TIME_FORMAT)


def _letters(device_ids):
    """{device_id: its open mapping's transaction letter}."""
    from apps.devices.models import DeviceMapping

    return dict(DeviceMapping.objects.filter(device_id__in=set(device_ids), to_date__isnull=True)
                .values_list("device_id", "transaction_letter"))


def held_by(hold, letters=None):
    letters = _letters([hold.device_id]) if letters is None else letters
    return {"device_id": hold.device_id, "device_letter": letters.get(hold.device_id) or "",
            "username": hold.user.username}


def hold_json(hold, zone):
    return {
        "sync_id": str(hold.pk), "vehicle_id": hold.vehicle_id, "vehicle_name": hold.vehicle.vehicle_name,
        "added_at": _local(hold.added_at, zone), "expires_at": _local(hold.expires_at, zone),
    }


def other_json(hold, zone, letters=None):
    return {"vehicle_id": hold.vehicle_id, "vehicle_name": hold.vehicle.vehicle_name,
            "added_at": _local(hold.added_at, zone), "expires_at": _local(hold.expires_at, zone),
            "held_by": held_by(hold, letters)}


def refusal_data(hold, zone):
    """The data of a `vehicle_in_other_cart` refusal."""
    return {"vehicle_id": hold.vehicle_id, "held_by": held_by(hold),
            "added_at": _local(hold.added_at, zone), "expires_at": _local(hold.expires_at, zone)}


def _mine(session, now):
    return list(live(now).filter(session=session).select_related("vehicle").order_by("added_at"))


def cart_json(session, zone, now):
    return [hold_json(h, zone) for h in _mine(session, now)]


# -- The cart calls ---------------------------------------------------------------------


def _multi_device(session):
    if not session.branch.is_multi_device:
        raise refuse("single_device_branch")


def refresh(session, now):
    """Restart the 10 minutes on all of this tablet's live holds."""
    return live(now).filter(session=session).update(expires_at=now + TTL, modified_on=now)


def add(session, values, *, now=None):
    """Put a vehicle in this tablet's cart. Returns (hold, created).

    Idempotent: the same sync_id again, or a vehicle already in this
    tablet's cart, answers that hold (with the clock restarted)."""
    from apps.fleet.models import Vehicle
    from apps.fleet.service_requests import in_warehouse_service
    from apps.rental import services, test_rides

    now = now or timezone.now()
    _multi_device(session)
    branch, sync_id, vehicle_id = session.branch, values["sync_id"], values["vehicle_id"]
    with transaction.atomic():
        vehicle = Vehicle.objects.select_for_update().filter(pk=vehicle_id, company=branch.company).first()
        if vehicle is None:
            raise refuse("unknown_vehicle")

        same = VehicleHold.objects.filter(pk=sync_id).select_related("session", "branch").first()
        if same is not None:
            if same.vehicle_id != vehicle.pk or same.device_id != session.device_id:
                raise refuse("sync_id_conflict")
            if same.released_at is None and _stale_reason(same, now) is None:
                refresh(session, now)
                same.refresh_from_db()
                return same, False
            raise refuse("sync_id_conflict")      # a released hold is not brought back: a new sync_id is

        current = (VehicleHold.objects.filter(vehicle=vehicle, released_at__isnull=True)
                   .select_related("session", "branch", "user").first())
        if current is not None:
            stale = _stale_reason(current, now)
            if stale is not None:
                _release([current], stale, now)
            elif current.session_id == session.pk or current.device_id == session.device_id:
                refresh(session, now)
                current.refresh_from_db()
                return current, False
            else:
                raise refuse("vehicle_in_other_cart", refusal_data(current, _zone(branch)))

        if vehicle.current_branch_id != branch.pk:
            raise refuse("vehicle_not_at_station")
        if services.active_rentals([vehicle.pk]):
            raise refuse("vehicle_already_rented")
        if test_rides.active_test_rides([vehicle.pk]):
            raise refuse("vehicle_on_test_ride")
        if in_warehouse_service([vehicle.pk]):
            raise refuse("vehicle_in_warehouse_service")

        try:
            with transaction.atomic():
                hold = VehicleHold.objects.create(
                    id=sync_id, company=branch.company, branch=branch, vehicle=vehicle,
                    device=session.device, session=session, user=session.user,
                    device_added_at=values.get("added_at"), added_at=now, expires_at=now + TTL,
                    created_by=session.user, modified_by=session.user,
                )
        except IntegrityError:
            raise refuse("sync_id_conflict") from None
        refresh(session, now)
        hold.refresh_from_db()
        _changed(branch.pk, vehicle.pk, "added", session.device_id)
    return hold, True


def remove(session, *, sync_id=None, vehicle_id=None, now=None):
    """Take one vehicle out of this tablet's cart. True if it was there."""
    now = now or timezone.now()
    _multi_device(session)
    holds = VehicleHold.objects.filter(session__device_id=session.device_id, released_at__isnull=True)
    holds = holds.filter(pk=sync_id) if sync_id is not None else holds.filter(vehicle_id=vehicle_id)
    holds = list(holds)
    with transaction.atomic():
        removed = _release(holds, HoldRelease.REMOVED, now)
        refresh(session, now)
    for hold in holds:
        _changed(hold.branch_id, hold.vehicle_id, "removed", session.device_id)
    return removed > 0


def clear(session, *, now=None):
    """Empty this tablet's cart. Returns how many were released."""
    now = now or timezone.now()
    _multi_device(session)
    holds = list(VehicleHold.objects.filter(session__device_id=session.device_id, released_at__isnull=True))
    removed = _release(holds, HoldRelease.CLEARED, now)
    for hold in holds:
        _changed(hold.branch_id, hold.vehicle_id, "removed", session.device_id)
    return removed


def status(session, zone, *, now=None):
    """This tablet's cart and the station's other live holds."""
    now = now or timezone.now()
    _multi_device(session)
    refresh(session, now)
    others = list(live(now).filter(branch=session.branch).exclude(session__device_id=session.device_id)
                  .select_related("vehicle", "user").order_by("added_at"))
    letters = _letters(h.device_id for h in others)
    return {
        "ttl_minutes": TTL_MINUTES,
        "server_time": _local(now, zone),
        "my_cart": cart_json(session, zone, now),
        "station_holds": [other_json(h, zone, letters) for h in others],
    }


# -- Booking (apps/rental/services.py::_line_refs) ----------------------------------------


def check_booking(session, vehicle_ids, *, replay=False, now=None):
    """Refuse a booking, add or replace naming a vehicle another tablet holds;
    on a replay, release that hold instead. Raises HoldRefused."""
    if session is None or not session.branch.is_multi_device:
        return
    now = now or timezone.now()
    others = list(live(now).filter(vehicle_id__in=list(vehicle_ids))
                  .exclude(session__device_id=session.device_id)
                  .select_related("vehicle", "user").order_by("vehicle_id"))
    if not others:
        return
    if replay:
        _release(others, HoldRelease.BOOKED, now)
        for hold in others:
            _changed(hold.branch_id, hold.vehicle_id, "removed", session.device_id)
        return
    raise refuse("vehicle_in_other_cart", refusal_data(others[0], _zone(session.branch)))


def release_booked(session, vehicle_ids, *, now=None):
    """After a booking, add or replace: this tablet's holds on those vehicles
    are done. Returns how many."""
    if session is None:
        return 0
    now = now or timezone.now()
    holds = list(VehicleHold.objects.filter(vehicle_id__in=list(vehicle_ids), released_at__isnull=True,
                                            device_id=session.device_id))
    released = _release(holds, HoldRelease.BOOKED, now)
    for hold in holds:
        _changed(hold.branch_id, hold.vehicle_id, "removed", session.device_id)
    return released


# -- The vehicles list (apps/fleet/services.py) -------------------------------------------


def station_view(branch, session, zone, now=None):
    """{vehicle_id: cart_hold} for the live holds at `branch`, as `session` sees them."""
    if not branch.is_multi_device:
        return {}
    now = now or timezone.now()
    holds = list(live(now).filter(branch=branch).select_related("user"))
    letters = _letters(h.device_id for h in holds)
    mine = getattr(session, "device_id", None)
    return {h.vehicle_id: {"mine": h.device_id == mine, "device_letter": letters.get(h.device_id) or "",
                           "username": h.user.username, "expires_at": _local(h.expires_at, zone)}
            for h in holds}


# -- Helpers ------------------------------------------------------------------------------


def _zone(branch):
    from core.timezones import zone_for

    return zone_for(branch.company)


def _changed(branch_id, vehicle_id, action, device_id):
    from apps.rental import station_sync

    station_sync.cart_changed(branch_id, vehicle_id, action, device_id)
