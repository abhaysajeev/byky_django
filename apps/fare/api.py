"""The operator app's fare download -- views only translate HTTP; the rules
are in apps/fare/services.py::device_fares.

POST /api/v1/{app}/fares sends every fare package of one vehicle type, whole
(base price, special prices, seasons), for the caller's own station. The app
runs the precedence itself (design/fares and offers/fare-schema.md section 4).
"""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.fare import services
from apps.fare.serializers import FaresRequest
from apps.portal.authentication import AppJWTAuthentication
from core.api import envelope, request_parts
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses

_PRICE = {"base_fare": "50.00", "grace_minutes": 5, "concurrent_interval_minutes": 10,
          "concurrent_fare": "10.00", "concurrent_grace_minutes": 0}

# The 200 example Swagger shows: a Monaco with a company fare (special prices
# and a season) and this station's own fare for part of the year.
_FARES_SAMPLE = {
    "generated_at": "2026-09-24T06:00:12+04:00",
    "business_date": "2026-09-24",
    "company": {"id": 1, "code": "0598", "timezone": "Asia/Dubai",
                "tax_type": "included", "discount_type": "before_tax"},
    "branch": {"id": 12, "code": "ADC1", "name": "Abu Dhabi Corniche 1"},
    "vehicle_type": {"id": 7, "name": "Monaco", "code": "MON", "category": "BYKY", "tax_percentage": "5.00"},
    "fares": [
        {
            "fare_id": 41, "version": 3, "level": "company", "package_minutes": 30,
            "valid_from": "2026-01-01", "valid_to": "2026-12-31",
            "base_price": _PRICE,
            "special_prices": [
                {"id": 101, "kind": "single_date", "on_date": "2026-12-02", "weekdays": [],
                 "start": "12:00", "end": "22:00", "price": {**_PRICE, "base_fare": "100.00", "concurrent_fare": "20.00"}},
                {"id": 102, "kind": "selected_days", "on_date": None, "weekdays": [5, 6],
                 "start": "14:00", "end": "22:00", "price": {**_PRICE, "base_fare": "80.00", "concurrent_fare": "15.00"}},
                {"id": 103, "kind": "every_day", "on_date": None, "weekdays": [],
                 "start": "16:00", "end": "20:00", "price": {**_PRICE, "base_fare": "70.00", "concurrent_fare": "12.00"}},
            ],
            "seasons": [
                {"id": 9, "name": "Spring", "start_date": "2026-04-10", "end_date": "2026-05-15",
                 "base_price": {**_PRICE, "base_fare": "55.00"},
                 "special_prices": [
                     {"id": 110, "kind": "every_day", "on_date": None, "weekdays": [],
                      "start": "16:00", "end": "20:00", "price": {**_PRICE, "base_fare": "90.00", "concurrent_fare": "15.00"}},
                 ]},
            ],
        },
        {
            "fare_id": 57, "version": 1, "level": "branch", "package_minutes": 30,
            "valid_from": "2026-10-01", "valid_to": "2026-12-31",
            "base_price": {**_PRICE, "base_fare": "45.00", "concurrent_fare": "8.00"},
            "special_prices": [], "seasons": [],
        },
    ],
}

_DESCRIPTION = """
Every fare package of one vehicle type, **whole**, for the caller's own station --
the station comes from the login session, never from the request.

**Sent:** active, approved fares for this vehicle type that have not ended
(`valid_to` >= today, company time), company-level ones and this station's own.

**Reading it** (the app runs this; the server refuses any fare that would make
it ambiguous, so the first match at each step is the answer):
1. Take the fare for the chosen `package_minutes` with `valid_from <= date <= valid_to`;
   when a `branch` and a `company` fare both match, the **branch** one wins.
2. A `single_date` special price on that date whose window holds the time wins.
3. Otherwise, if a season covers the date, use **that season's** `base_price` and
   `special_prices`; else the fare's own.
4. In that list: `selected_days` (weekday in `weekdays`), then `every_day`, then `base_price`.

**Formats:** weekdays Monday=0 ... Sunday=6; times local to `company.timezone`,
window includes `start`, excludes `end`, `"24:00"` is midnight; dates inclusive;
money is a 2-decimal string.
"""


class FaresView(APIView):
    """POST /api/v1/{app}/fares -- operator app only."""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Operator Fares"],
        summary="Every fare package of one vehicle type, for this station",
        description=_DESCRIPTION,
        request=envelope_request("FaresEnvelope", FaresRequest),
        responses=envelope_responses(
            (200, "ok", "Fares.", _FARES_SAMPLE),
            (400, "invalid_request", "vehicle_type_id is required.",
             {"errors": {"vehicle_type_id": "is required"}}),
            (400, "unknown_vehicle_type", "That vehicle type is not set up for this station.", {}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "device_not_mapped", "This device has no station.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        branch = request.auth.branch
        if branch is None:
            return envelope("device_not_mapped", "This device has no station.", http_status=409)

        _, request_data = request_parts(request)
        form = FaresRequest(data=request_data)
        form.is_valid(raise_exception=True)

        try:
            data = services.device_fares(branch, form.validated_data["vehicle_type_id"])
        except services.UnknownVehicleType:
            return envelope("unknown_vehicle_type", "That vehicle type is not set up for this station.",
                            http_status=400)
        return envelope("ok", "Fares.", data)
