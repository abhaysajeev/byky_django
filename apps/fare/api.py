"""The operator app's fare download -- views only translate HTTP; the rules
are in apps/fare/services.py::device_fares.

POST /api/v1/{app}/fares sends the fares a station needs for one working day,
whole, in an order where the app takes the first match at every level. The
server picks the fares by date; the app prices each rental by its time
(design/fares and offers/fare-schema.md section 4).
"""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.fare import package_device, services
from apps.fare.serializers import FaresRequest, PackagesRequest
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, request_parts, session_station
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses


def _price(base_fare, concurrent_fare="10.00"):
    return {"base_fare": base_fare, "grace_minutes": 5, "concurrent_interval_minutes": 10,
            "concurrent_fare": concurrent_fare, "concurrent_grace_minutes": 0}


def _special(pk, kind, start, end, base_fare, *, on_date=None, weekdays=()):
    return {"id": pk, "kind": kind, "on_date": on_date, "weekdays": list(weekdays),
            "start": start, "end": end, "price": _price(base_fare)}


# The 200 example Swagger shows: Monaco 30 min on 30 Sep -- this station's own
# fare starts tomorrow, the company fare runs all year with a lunch price
# inside the day price, a holiday, and Eid inside Summer.
_FARES_SAMPLE = {
    "date": "2026-09-30",
    "vehicle_types": [
        {
            "vehicle_type_id": 56,
            "packages": [
                {
                    "package_minutes": 30,
                    "fares": [
                        {
                            "fare_id": 57, "version": 1, "valid_from": "2026-10-01", "valid_to": "2026-12-31",
                            "price": _price("55.00", "8.00"), "special_prices": [], "seasons": [],
                        },
                        {
                            "fare_id": 41, "version": 3, "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                            "price": _price("50.00"),
                            "special_prices": [
                                _special(101, "single_date", "18:00", "22:00", "100.00", on_date="2026-12-02"),
                                _special(102, "selected_days", "14:00", "22:00", "80.00", weekdays=[5, 6]),
                                _special(104, "every_day", "12:00", "14:00", "90.00"),
                                _special(103, "every_day", "08:00", "20:00", "60.00"),
                            ],
                            "seasons": [
                                {"id": 12, "name": "Eid", "start_date": "2026-04-10", "end_date": "2026-05-15",
                                 "price": _price("95.00"), "special_prices": []},
                                {"id": 9, "name": "Summer", "start_date": "2026-02-01", "end_date": "2026-09-30",
                                 "price": _price("55.00"),
                                 "special_prices": [_special(110, "every_day", "16:00", "20:00", "75.00")]},
                            ],
                        },
                    ],
                },
            ],
        },
    ],
}

_DESCRIPTION = """
The fares this station needs for one working day. Call it at login with that
day's `date`; calling again when online is safe -- each answer **replaces**
what the app holds for the same filters. Clear the fare cache at every login.

**Request** (`request_data`): `date` required (`YYYY-MM-DD`, the company's local
date, taken as sent); `vehicle_type_id` and `package_minutes` optional -- each
only narrows the answer. The station comes from the login session.

**Sent:** active, approved fares of active, approved vehicle types, company-level
and this station's own, in force on `date` **or the day after** (for rentals
after midnight). Each fare comes whole.

**Pricing a rental** -- vehicle type, package, and the rental's **start** date and
time; the start fixes the price for the whole rental. At every step take the
**first** match in the order sent:
1. **Fare:** in that vehicle type and package's `fares`, the first whose
   `valid_from <= date <= valid_to`. None: no fare -- do not rent.
2. **Single date:** in that fare's `special_prices`, the first `single_date` with
   `on_date` = the date and `start <= time < end`. Found: use its `price`. Done.
3. **List:** the first of the fare's `seasons` with `start_date <= date <= end_date`
   -- use that season's `price` and `special_prices`; none, the fare's own.
4. **Special price:** in that list's `special_prices`, the first `selected_days`
   (weekday in `weekdays`) or `every_day` entry with `start <= time < end`
   (skip `single_date`). Found: use its `price`; else the list's own `price`.

The order already puts the station's fare before the company's, the shortest
season first, and single date, selected days, every day with the shortest
window first -- so the first match is always the right one.

**On the bill:** `fare_id`, `version`, and the season `id` and special price `id`
used (null when not used).

**A `price`** is five numbers, all used by the tablet, which works out the
amounts it sends at return (the server stores them, it never recalculates):
- `base_fare` -- the package price, **VAT included** (fares include VAT);
- `grace_minutes` -- free minutes after the package ends before extra time starts;
- `concurrent_interval_minutes` / `concurrent_fare` -- extra time is charged per
  started interval at this price;
- `concurrent_grace_minutes` -- the tablet's own grace within extra time, used in
  its overtime calculation.

The three concurrent values are optional: each is `null` when not set, and
always `null` for a direct-rent vehicle type. The tablet decides what an unset
value means.

**Formats:** weekdays Monday=0 ... Sunday=6; times `HH:MM` in the company's
timezone, `"24:00"` is midnight; dates inclusive; money a 2-decimal string.
An unknown vehicle type is an error; a known one with no fare is an empty list.
"""


class FaresView(APIView):
    """POST /api/v1/{app}/fares -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Fares"],
        summary="The station's fares for a working day",
        description=_DESCRIPTION,
        request=envelope_request("FaresEnvelope", FaresRequest),
        responses=envelope_responses(
            (200, "ok", "Fares.", _FARES_SAMPLE),
            (400, "invalid_request", "date is required.", {"errors": {"date": "is required"}}),
            (400, "unknown_vehicle_type", "That vehicle type is not set up for this station.", {}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "device_not_mapped", "This device has no station.", {}),
            (409, "branch_inactive", "This station is closed.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        branch, refused = session_station(request)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = FaresRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data

        try:
            data = services.device_fares(branch, values["date"],
                                         vehicle_type_id=values.get("vehicle_type_id"),
                                         package_minutes=values.get("package_minutes"))
        except services.UnknownVehicleType:
            return envelope("unknown_vehicle_type", "That vehicle type is not set up for this station.",
                            http_status=400)
        return envelope("ok", "Fares.", data)



# -- Packages --------------------------------------------------------------------------

_PACKAGES_SAMPLE = {
    "date": "2026-10-19",
    "packages": [
        {
            "package_id": 14, "version": "2026-10-18T09:12:40.120311+04:00",
            "code": "ADD-ECAR", "name": "Kids E-Car Add-On", "level": "branch",
            "valid_from": "2026-10-01", "valid_to": "2027-04-30",
            "qualifies": {"promotion_for": "quantity", "lower": "1", "upper": "3"},
            "promotion_type": "quantity",
            "items": [
                {"item_id": 40, "vehicle_type_id": 56, "package_minutes": 45, "value": None},
                {"item_id": 41, "vehicle_type_id": 57, "package_minutes": 30, "value": None},
            ],
            "free_items": {"mode": "package_price", "items": [
                {"free_item_id": 7, "vehicle_type_id": 60, "package_minutes": 15, "value": "20.000"}]},
            "time_slabs": [
                {"slab_id": 3, "on_date": None, "weekday": 0, "start": "10:00", "end": "13:00",
                 "vehicle_type_id": 60, "package_minutes": 15, "value": "20.000"}],
            "free_item_note": "",
        },
        {
            "package_id": 12, "version": "2026-10-17T16:02:11.009812+04:00",
            "code": "SCOOT-2", "name": "Couple Scooter Ride", "level": "company",
            "valid_from": "2026-10-01", "valid_to": "2026-12-31",
            "qualifies": {"promotion_for": "quantity", "lower": "2", "upper": "2"},
            "promotion_type": "package_price",
            "items": [{"item_id": 31, "vehicle_type_id": 58, "package_minutes": 15, "value": "22.500"}],
            "free_items": None, "time_slabs": [], "free_item_note": "",
        },
    ],
    "by_vehicle_type": {"56": [14], "57": [14], "58": [12], "60": [14]},
}

_PACKAGES_DESCRIPTION = """
The packages this station can offer, for one working day -- the same contract
as `fares`: call it at login with that day's `date` and again when online;
each answer **replaces** what the app holds. Clear the package cache at every
login.

**Request** (`request_data`): `date` required (`YYYY-MM-DD`, company date);
`vehicle_type_id` optional -- only packages that mention it (as an item, a
free item or a time slab). The station comes from the login session.

**Sent:** active, approved packages that cover this station -- the company's,
this station's own (`level` `branch`) and its location's (`location`) -- in
force on `date` **or the day after**. Most specific first (`branch`, then
`location`, then `company`), then by code. A package is sent **even when an
item has no fare at this station**: check the fares before offering it.

**A package** comes whole:
* `qualifies` -- the band a rental must fall in: `promotion_for` `quantity`
  (a number of vehicles; `lower` / `upper` whole numbers) or `amount` (AED).
* `promotion_type` and each item's `value`:
  `amount` -- AED off; `percentage` -- % off; `package_price` -- the price
  for that vehicle type and package time; `quantity` -- no value (`null`), the
  reward is `free_items`.
* `free_items` -- `null` unless `quantity` with free items: `mode` `free`
  (each `value` a free count) or `package_price` (each `value` a price).
* `time_slabs` -- `[]` unless the package uses them: `on_date` (or `null`),
  `weekday` (Monday=0 ... Sunday=6, `null` = every day), `start` / `end`
  (`HH:MM`, company time) and the vehicle type, package time and value for
  that window (its meaning follows `free_items.mode`).
* `version` -- when the package last changed; a different value means the
  cached copy is stale.

`by_vehicle_type` lists, per vehicle type id, the packages that mention it,
for offering packages as soon as a vehicle is scanned.

**On the order:** a vehicle line names its package as `offer_id` (orders/book,
add, replace). The server refuses `package_not_available` when that package is
paused, does not cover this station or is not in force on the line's start
date. Money is a 3-decimal string. Nothing here prices a rental: the app
applies the package and sends the `base_fare` it agreed, as today.
"""


class PackagesView(APIView):
    """POST /api/v1/{app}/packages -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Fares"],
        summary="The station's packages for a working day",
        description=_PACKAGES_DESCRIPTION,
        request=envelope_request("PackagesEnvelope", PackagesRequest),
        responses=envelope_responses(
            (200, "ok", "Packages.", _PACKAGES_SAMPLE),
            (400, "invalid_request", "date is required.", {"errors": {"date": "is required"}}),
            (400, "unknown_vehicle_type", "That vehicle type is not set up for this station.", {}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "device_not_mapped", "This device has no station.", {}),
            (409, "branch_inactive", "This station is closed.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        branch, refused = session_station(request)
        if refused:
            return refused

        _, request_data = request_parts(request)
        form = PackagesRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        try:
            if values.get("vehicle_type_id") is not None:
                services.check_vehicle_type(branch, values["vehicle_type_id"])
        except services.UnknownVehicleType:
            return envelope("unknown_vehicle_type", "That vehicle type is not set up for this station.",
                            http_status=400)
        return envelope("ok", "Packages.", package_device.device_packages(
            branch, values["date"], vehicle_type_id=values.get("vehicle_type_id")))
