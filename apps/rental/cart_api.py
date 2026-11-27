"""POST /api/v1/{app}/cart/... -- operator app only (apps/rental/holds.py).

Live calls, never parked: a cart is only worth anything while the tablet is
online. Offline, the tablet carries on without it and books as today -- the
booking stays the authority.
"""

from django.utils import timezone
from drf_spectacular.utils import extend_schema

from apps.rental import holds
from apps.rental.api import _COMMON, _OperatorView
from apps.rental.serializers import CartAddRequest, CartEmptyRequest, CartRemoveRequest
from core.api import envelope, request_parts
from core.schema import envelope_request, envelope_responses
from core.timezones import zone_for

TAGS = ["Operator Cart"]

_HOLD = {"sync_id": "019a8b2c-1f00-7000-8000-0000000000c1", "vehicle_id": 812, "vehicle_name": "KB 113",
         "added_at": "2026-11-18 14:05:10", "expires_at": "2026-11-18 14:15:10"}
_HELD_BY = {"device_id": 85, "device_letter": "B", "username": "c-2"}

_RULES = """
**Multi-device stations only** -- the branch master's `is_multi_device`, sent
in the login reply's `branch` block. Elsewhere every cart call answers
`single_device_branch`: do not show the cart there.

A vehicle in this tablet's cart is **held** for 10 minutes; while it is, no
other tablet at the station can cart it, and booking, add or replace with it
from another tablet are refused `vehicle_in_other_cart`. **Any cart call
restarts the 10 minutes on all of this tablet's holds**, so a cart in use
never expires; `expires_at` is server time in company time. Booking the
vehicle (or add / replace) releases this tablet's hold on it. Signing out --
logout, force logout, a newer sign-in -- releases every hold of the tablet.

The station's other tablets get a **silent** push (data only, no tray entry):
`{type: cart_changed, vehicle_id, action: added|removed, branch_id, at}` --
refresh with `cart/status` or `vehicles` (`cart_hold` on each vehicle).

Offline: carry on without the cart and book as usual; the booking decides.
"""

_ADD_DESCRIPTION = """
One vehicle into this tablet's cart.

* `sync_id` (UUIDv7) becomes the hold's id. Sent again -- or the same vehicle
  already in this tablet's cart -- answers `duplicate` with that hold, its 10
  minutes restarted.
* `added_at` is the tablet's clock, kept for the record; the 10 minutes run
  on the server's.

Refusals: `vehicle_in_other_cart` (`data`: the vehicle, who holds it, since
when and until when), `vehicle_already_rented`, `vehicle_on_test_ride`,
`vehicle_in_warehouse_service`, `vehicle_not_at_station`, `unknown_vehicle`,
`sync_id_conflict` (that id is another vehicle's or tablet's hold, or one
already released -- use a new id), `single_device_branch`.
""" + _RULES

_REMOVE_DESCRIPTION = """
One vehicle out of this tablet's cart, by the hold's `sync_id` **or** by
`vehicle_id`. Always `ok` -- `removed: false` if it was not in the cart any
more (expired, booked, removed) -- so it is safe to resend.
""" + _RULES

_CLEAR_DESCRIPTION = """
Empty this tablet's cart -- the customer walked away. `removed` is how many
were released.
""" + _RULES

_STATUS_DESCRIPTION = """
This tablet's cart (`my_cart`) and the vehicles other tablets at this station
hold now (`station_holds`, with who holds them). Call it at login, on resume,
on reconnect and on a `cart_changed` push. It restarts this tablet's 10
minutes too.
""" + _RULES


def _rows(*codes):
    return [(holds.MESSAGES[c][0], c, holds.MESSAGES[c][1], {}) for c in codes]


_IN_OTHER_CART = (409, "vehicle_in_other_cart", holds.MESSAGES["vehicle_in_other_cart"][1], {
    "vehicle_id": 812, "held_by": _HELD_BY, "added_at": "2026-11-18 14:03:00", "expires_at": "2026-11-18 14:13:00",
})


def _refused(refusal):
    return envelope(refusal.code, refusal.message, refusal.data, http_status=refusal.status)


class _CartView(_OperatorView):
    """Operator app, a mapped open station; never parked."""

    def begin(self, request, app):
        """(session, branch, zone, None) or (None, None, None, the reply)."""
        branch, refused = self.station(request, app)
        if refused:
            return None, None, None, refused
        return request.auth, branch, zone_for(branch.company), None


class CartAddView(_CartView):
    """POST /api/v1/{app}/cart/add -- operator app only."""

    @extend_schema(
        tags=TAGS, summary="Put a vehicle in this tablet's cart (hold it 10 minutes)",
        description=_ADD_DESCRIPTION,
        request=envelope_request("CartAddEnvelope", CartAddRequest),
        responses=envelope_responses(
            (200, "ok", "Added to the cart.", {"hold": _HOLD, "cart": [_HOLD]}),
            (200, "duplicate", "Already in the cart.", {"hold": _HOLD, "cart": [_HOLD]}),
            (400, "invalid_request", "sync_id must be a UUIDv7.", {"errors": {"sync_id": "must be a UUIDv7"}}),
            _IN_OTHER_CART,
            *_rows("vehicle_already_rented", "vehicle_on_test_ride", "vehicle_in_warehouse_service",
                   "vehicle_not_at_station", "unknown_vehicle", "sync_id_conflict", "single_device_branch"),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        session, branch, zone, refused = self.begin(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = CartAddRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        if values.get("added_at") is not None:
            values["added_at"] = timezone.make_aware(values["added_at"], zone)
        now = timezone.now()
        try:
            hold, created = holds.add(session, values, now=now)
        except holds.HoldRefused as refusal:
            return _refused(refusal)
        data = {"hold": holds.hold_json(hold, zone), "cart": holds.cart_json(session, zone, now)}
        return envelope("ok", "Added to the cart.", data) if created else envelope(
            "duplicate", "Already in the cart.", data)


class CartRemoveView(_CartView):
    """POST /api/v1/{app}/cart/remove -- operator app only."""

    @extend_schema(
        tags=TAGS, summary="Take a vehicle out of this tablet's cart", description=_REMOVE_DESCRIPTION,
        request=envelope_request("CartRemoveEnvelope", CartRemoveRequest),
        responses=envelope_responses(
            (200, "ok", "Removed from the cart.", {"removed": True, "cart": []}),
            (400, "invalid_request", "send sync_id or vehicle_id, one of them.",
             {"errors": {"sync_id": "send sync_id or vehicle_id, one of them"}}),
            *_rows("single_device_branch"),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        session, branch, zone, refused = self.begin(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = CartRemoveRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        now = timezone.now()
        try:
            removed = holds.remove(session, sync_id=values.get("sync_id"), vehicle_id=values.get("vehicle_id"),
                                   now=now)
        except holds.HoldRefused as refusal:
            return _refused(refusal)
        return envelope("ok", "Removed from the cart." if removed else "Not in the cart.",
                        {"removed": removed, "cart": holds.cart_json(session, zone, now)})


class CartClearView(_CartView):
    """POST /api/v1/{app}/cart/clear -- operator app only."""

    @extend_schema(
        tags=TAGS, summary="Empty this tablet's cart", description=_CLEAR_DESCRIPTION,
        request=envelope_request("CartClearEnvelope", CartEmptyRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Cart cleared.", {"removed": 3, "cart": []}),
            *_rows("single_device_branch"),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        session, branch, zone, refused = self.begin(request, app)
        if refused:
            return refused
        try:
            removed = holds.clear(session)
        except holds.HoldRefused as refusal:
            return _refused(refusal)
        return envelope("ok", "Cart cleared.", {"removed": removed, "cart": []})


class CartStatusView(_CartView):
    """POST /api/v1/{app}/cart/status -- operator app only."""

    @extend_schema(
        tags=TAGS, summary="This tablet's cart and the station's other holds", description=_STATUS_DESCRIPTION,
        request=envelope_request("CartStatusEnvelope", CartEmptyRequest, request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Cart.", {
                "ttl_minutes": holds.TTL_MINUTES, "server_time": "2026-11-18 14:07:02", "my_cart": [_HOLD],
                "station_holds": [{"vehicle_id": 905, "vehicle_name": "KB 120", "added_at": "2026-11-18 14:06:00",
                                   "expires_at": "2026-11-18 14:16:00", "held_by": _HELD_BY}],
            }),
            *_rows("single_device_branch"),
            *_COMMON,
        ),
    )
    def post(self, request, app):
        session, branch, zone, refused = self.begin(request, app)
        if refused:
            return refused
        try:
            data = holds.status(session, zone)
        except holds.HoldRefused as refusal:
            return _refused(refusal)
        return envelope("ok", "Cart.", data)
